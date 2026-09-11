/**
 * TASKS MANAGEMENT & FEEDBACK LOOP
 */

async function loadTasks() {
  try {
    const res = await fetch('/api/tasks');
    const data = await res.json();
    if (data.success) {
      AppState.tasks = data.tasks;
      renderTasks();
      const countEl = document.getElementById('navTaskCount');
      if (countEl) countEl.innerText = data.tasks.filter(t => t.status !== 'done').length;
    }
  } catch (err) {
    console.error('Failed to load tasks', err);
  }
}

function setTaskView(mode) {
  AppState.viewMode = mode;
  document.getElementById('viewModeKanban').classList.toggle('active', mode === 'kanban');
  document.getElementById('viewModeList').classList.toggle('active', mode === 'list');
  document.getElementById('kanbanBoard').classList.toggle('hidden', mode !== 'kanban');
  document.getElementById('taskListView').classList.toggle('hidden', mode !== 'list');
  renderTasks();
}

function renderTasks() {
  if (AppState.viewMode === 'kanban') {
    renderKanban();
  } else {
    renderTaskList();
  }
}

function renderKanban() {
  const cols = {
    todo: document.getElementById('cards-todo'),
    in_progress: document.getElementById('cards-in_progress'),
    done: document.getElementById('cards-done')
  };
  const counts = {
    todo: document.getElementById('count-todo'),
    in_progress: document.getElementById('count-in_progress'),
    done: document.getElementById('count-done')
  };

  Object.values(cols).forEach(c => { if (c) c.innerHTML = ''; });
  const colCounts = { todo: 0, in_progress: 0, done: 0 };

  AppState.tasks.forEach(task => {
    const col = cols[task.status] || cols.todo;
    colCounts[task.status] = (colCounts[task.status] || 0) + 1;
    if (col) col.appendChild(createTaskCard(task));
  });

  if (counts.todo) counts.todo.innerText = colCounts.todo || 0;
  if (counts.in_progress) counts.in_progress.innerText = colCounts.in_progress || 0;
  if (counts.done) counts.done.innerText = colCounts.done || 0;
}

function renderTaskList() {
  const container = document.getElementById('taskListItems');
  if (!container) return;
  container.innerHTML = '';

  if (AppState.tasks.length === 0) {
    container.innerHTML = '<div class="card-desc" style="text-align:center; padding: 2rem;">No tasks yet. Use Quick Capture (Ctrl+K) or click "+ New Task".</div>';
    return;
  }

  AppState.tasks.forEach(task => {
    container.appendChild(createTaskCard(task));
  });
}

function createTaskCard(task) {
  const card = document.createElement('div');
  card.className = 'task-card';
  card.dataset.id = task.id;

  const tagsHtml = (task.tags || '')
    .split(',')
    .map(t => t.trim())
    .filter(Boolean)
    .map(t => `<span class="task-tag">#${escapeHtml(t)}</span>`)
    .join('');

  const subtasks = task.subtasks || [];
  let subtasksHtml = '';
  if (subtasks.length > 0) {
    subtasksHtml = `
      <div class="subtasks-box">
        ${subtasks.map((st, idx) => `
          <label class="subtask-item ${st.completed ? 'done' : ''}">
            <input type="checkbox" ${st.completed ? 'checked' : ''} onchange="toggleSubtask(${task.id}, ${idx}, this.checked)">
            <span>${escapeHtml(st.title)}</span>
          </label>
        `).join('')}
      </div>
    `;
  }

  // AI Suggestion Banner with Feedback Buttons
  let aiChipHtml = '';
  const aiCtx = task.ai_suggestion_context;
  if (aiCtx && aiCtx.rationale && aiCtx.user_accepted === null) {
    aiChipHtml = `
      <div class="ai-recommendation-chip">
        <div class="ai-chip-header">
          <span>🧠 Learned Workflow Suggestion</span>
          <span>Priority: ${escapeHtml(aiCtx.suggested_priority || task.priority)}</span>
        </div>
        <div class="ai-chip-rationale">${escapeHtml(aiCtx.rationale)}</div>
        <div class="ai-chip-actions">
          <button class="btn-chip-accept" onclick="acceptTaskSuggestion(${task.id})">✓ Looks Good</button>
          <button class="btn-chip-correct" onclick="openCorrectionModal(${task.id})">✏️ Correct This</button>
        </div>
      </div>
    `;
  } else if (aiCtx && aiCtx.user_accepted === true) {
    aiChipHtml = `<div style="font-size:0.7rem; color:var(--success); margin-top:3px;">✓ Preference reinforced</div>`;
  } else if (aiCtx && aiCtx.user_accepted === false) {
    aiChipHtml = `<div style="font-size:0.7rem; color:var(--purple); margin-top:3px;">✏️ Adapted: ${escapeHtml(aiCtx.user_correction || 'Corrected')}</div>`;
  }

  card.innerHTML = `
    <div class="task-card-header">
      <span class="task-title">${escapeHtml(task.title)}</span>
      <span class="task-badge-priority priority-${task.priority}">${task.priority}</span>
    </div>
    ${task.description ? `<p class="task-desc">${escapeHtml(task.description)}</p>` : ''}
    ${subtasksHtml}
    ${aiChipHtml}
    <div class="task-meta">
      ${task.due_date ? `<span class="task-due">📅 ${task.due_date}</span>` : ''}
      ${tagsHtml}
    </div>
    <div class="task-card-footer">
      <select class="form-control" style="font-size:0.75rem; padding: 2px 6px; width: auto;" onchange="changeTaskStatus(${task.id}, this.value)">
        <option value="todo" ${task.status === 'todo' ? 'selected' : ''}>To Do</option>
        <option value="in_progress" ${task.status === 'in_progress' ? 'selected' : ''}>In Progress</option>
        <option value="done" ${task.status === 'done' ? 'selected' : ''}>Completed</option>
      </select>
      <div class="task-actions">
        <button class="btn-icon" onclick="openEditTaskModal(${task.id})" title="Edit Task">✏️</button>
        <button class="btn-icon" onclick="deleteTaskItem(${task.id})" title="Delete Task">🗑️</button>
      </div>
    </div>
  `;
  return card;
}

// Subtask Toggle
async function toggleSubtask(taskId, subtaskIdx, isDone) {
  const task = AppState.tasks.find(t => t.id === taskId);
  if (!task || !task.subtasks) return;

  task.subtasks[subtaskIdx].completed = isDone;
  try {
    await fetch(`/api/tasks/${taskId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subtasks: task.subtasks })
    });
    renderTasks();
  } catch (err) {
    showToast('Failed to update subtask', 'error');
  }
}

// Status Change
async function changeTaskStatus(taskId, newStatus) {
  try {
    const res = await fetch(`/api/tasks/${taskId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus })
    });
    if (res.ok) {
      loadTasks();
      loadMetrics();
    }
  } catch (err) {
    showToast('Failed to update status', 'error');
  }
}

// Task Deletion
async function deleteTaskItem(taskId) {
  if (!confirm('Are you sure you want to delete this task?')) return;
  try {
    const res = await fetch(`/api/tasks/${taskId}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('Task removed', 'info');
      loadTasks();
      loadMetrics();
    }
  } catch (err) {
    showToast('Failed to delete task', 'error');
  }
}

// FEEDBACK LOOP HANDLERS (PRD §5.4)
async function acceptTaskSuggestion(taskId) {
  try {
    const res = await fetch(`/api/tasks/${taskId}/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'accepted' })
    });
    const data = await res.json();
    if (data.success) {
      showToast('✓ Suggestion accepted! Preference weight reinforced.', 'success');
      loadTasks();
      loadMemories();
      loadMetrics();
    }
  } catch (err) {
    showToast('Failed to record feedback', 'error');
  }
}

function openCorrectionModal(taskId) {
  const task = AppState.tasks.find(t => t.id === taskId);
  if (!task) return;

  document.getElementById('feedbackTaskId').value = taskId;
  const orig = task.ai_suggestion_context?.suggested_priority || task.priority;
  document.getElementById('feedbackOriginalSuggestion').innerText = `Priority: ${orig.toUpperCase()}`;
  document.getElementById('feedbackCorrectionInput').value = '';
  openModal('feedbackCorrectionModal');
}

async function submitTaskCorrection() {
  const taskId = document.getElementById('feedbackTaskId').value;
  const correction = document.getElementById('feedbackCorrectionInput').value.trim();

  if (!correction) {
    showToast('Please describe the correction or preference rule', 'error');
    return;
  }

  try {
    const res = await fetch(`/api/tasks/${taskId}/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'corrected', correction })
    });
    const data = await res.json();
    if (data.success) {
      closeModal('feedbackCorrectionModal');
      showToast('🧠 Agent learned your correction rule and updated memory!', 'success');
      loadTasks();
      loadMemories();
      loadMetrics();
    }
  } catch (err) {
    showToast('Failed to submit correction', 'error');
  }
}

// New Task Modal
function openNewTaskModal() {
  document.getElementById('editTaskId').value = '';
  document.getElementById('taskModalTitle').innerText = 'Create New Task';
  document.getElementById('taskTitleInput').value = '';
  document.getElementById('taskDescInput').value = '';
  document.getElementById('taskPriorityInput').value = 'medium';
  document.getElementById('taskDueDateInput').value = '';
  document.getElementById('taskTagsInput').value = '';
  openModal('taskModal');
}

function openEditTaskModal(taskId) {
  const task = AppState.tasks.find(t => t.id === taskId);
  if (!task) return;

  document.getElementById('editTaskId').value = task.id;
  document.getElementById('taskModalTitle').innerText = 'Edit Task';
  document.getElementById('taskTitleInput').value = task.title;
  document.getElementById('taskDescInput').value = task.description || '';
  document.getElementById('taskPriorityInput').value = task.priority || 'medium';
  document.getElementById('taskDueDateInput').value = task.due_date || '';
  document.getElementById('taskTagsInput').value = task.tags || '';
  openModal('taskModal');
}

async function saveTaskModal() {
  const id = document.getElementById('editTaskId').value;
  const title = document.getElementById('taskTitleInput').value.trim();
  if (!title) {
    showToast('Title is required', 'error');
    return;
  }

  const payload = {
    title,
    description: document.getElementById('taskDescInput').value.trim(),
    priority: document.getElementById('taskPriorityInput').value,
    due_date: document.getElementById('taskDueDateInput').value || null,
    tags: document.getElementById('taskTagsInput').value.trim()
  };

  try {
    let res;
    if (id) {
      res = await fetch(`/api/tasks/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    } else {
      res = await fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    }
    const data = await res.json();
    if (data.success) {
      closeModal('taskModal');
      showToast(id ? 'Task updated!' : 'Task created with AI enhancements!', 'success');
      loadTasks();
      loadMetrics();
    }
  } catch (err) {
    showToast('Failed to save task', 'error');
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
