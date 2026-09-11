/**
 * TRANSPARENCY DASHBOARD & MEMORY PROVENANCE
 * Full visibility, provenance timelines, and CRUD controls over learned rules
 */

let activeMemoryFilter = 'all';

async function loadMemories() {
  try {
    const res = await fetch('/api/memory');
    const data = await res.json();
    if (data.success) {
      AppState.memories = data.memories;
      renderMemories();
      const badge = document.getElementById('navMemoryCount');
      if (badge) badge.innerText = `${data.memories.length} rules`;
    }
  } catch (err) {
    console.error('Failed to load memories', err);
  }
}

function filterMemories(category) {
  activeMemoryFilter = category;
  document.querySelectorAll('.filter-pill').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.category === category);
  });
  renderMemories();
}

function renderMemories() {
  const container = document.getElementById('memoriesContainer');
  if (!container) return;
  container.innerHTML = '';

  const filtered = activeMemoryFilter === 'all'
    ? AppState.memories
    : AppState.memories.filter(m => m.category === activeMemoryFilter);

  if (filtered.length === 0) {
    container.innerHTML = '<div class="card-desc" style="text-align:center; padding: 2.5rem;">No memories in this category yet. As you use Cognito and correct suggestions, learned habits will appear here!</div>';
    return;
  }

  filtered.forEach(mem => {
    const card = document.createElement('div');
    card.className = 'memory-card';

    const confPct = Math.round((mem.confidence_weight || 0.7) * 100);
    const updateCount = mem.update_count || 0;

    card.innerHTML = `
      <div class="memory-left">
        <div class="badge-row">
          <span class="cat-badge ${mem.category}">${mem.category}</span>
          <span class="conf-badge">Confidence: ${confPct}%</span>
          ${updateCount > 0 ? `<span class="update-badge">${updateCount} adaptation${updateCount > 1 ? 's' : ''}</span>` : ''}
        </div>
        <div class="memory-text">${escapeHtml(mem.content)}</div>
        <div class="memory-provenance-hint">
          <span>Source: ${escapeHtml(mem.source_context || 'System initialization')}</span>
        </div>
      </div>
      <div class="memory-right">
        <button class="btn-why" onclick="openProvenanceModal(${mem.id})">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>
          Why this exists
        </button>
        <button class="btn-icon" onclick="openEditMemoryModal(${mem.id})" title="Edit Memory">✏️</button>
        <button class="btn-icon" onclick="deleteMemoryItem(${mem.id})" title="Delete Memory">🗑️</button>
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
