/**
 * COGNITO APP CORE JS
 * Navigation, Modal Management, Global State, Toast Notifications
 */

const AppState = {
  currentTab: 'tasks',
  tasks: [],
  notes: [],
  memories: [],
  metrics: null,
  settings: {},
  viewMode: 'kanban'
};

// Initialization
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initShortcuts();
  loadAllData();
});

// Theme Management
function initTheme() {
  const saved = localStorage.getItem('cognito_theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  updateThemeIcon(saved);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('cognito_theme', next);
  updateThemeIcon(next);
}

function updateThemeIcon(theme) {
  const icon = document.getElementById('themeIcon');
  if (icon) icon.innerText = theme === 'dark' ? '🌙' : '☀️';
}

// Global Keyboard Shortcuts
function initShortcuts() {
  window.addEventListener('keydown', (e) => {
    // Ctrl + K -> Open Quick Capture
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      openQuickCapture();
    }
    // Escape -> Close any active modal
    if (e.key === 'Escape') {
      closeAllModals();
    }
  });
}

// Navigation Tabs
function switchTab(tabId) {
  AppState.currentTab = tabId;
  
  // Update sidebar buttons
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });

  // Update view sections
  document.querySelectorAll('.tab-view').forEach(view => {
    view.classList.toggle('active', view.id === `${tabId}-tab`);
  });

  // Load specific tab data if needed
  if (tabId === 'tasks') renderTasks();
  if (tabId === 'notes') renderNotes();
  if (tabId === 'memories') loadMemories();
  if (tabId === 'settings') loadSettings();
}

// Modal Helpers
function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.add('active');
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.remove('active');
}

function closeAllModals() {
  document.querySelectorAll('.modal-overlay').forEach(m => m.classList.remove('active'));
}

// Toast Notifications
function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerText = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 200);
  }, 3500);
}

// Quick Capture Handler
function openQuickCapture() {
  openModal('quickCaptureModal');
  const input = document.getElementById('quickCaptureInput');
  if (input) {
    input.value = '';
    setTimeout(() => input.focus(), 80);
  }
}

function handleQuickCaptureKeyDown(e) {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    submitQuickCapture();
  }
}

async function submitQuickCapture() {
  const input = document.getElementById('quickCaptureInput');
  const text = input ? input.value.trim() : '';
  if (!text) return;

  try {
    const res = await fetch('/api/tasks/quick-capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });
    const data = await res.json();
    if (data.success) {
      closeModal('quickCaptureModal');
      showToast(data.type === 'task' ? '⚡ Task captured with AI enhancement!' : '📝 Note captured & indexed!', 'success');
      loadAllData();
      if (data.type === 'task') switchTab('tasks');
      else switchTab('notes');
    } else {
      showToast(data.error || 'Failed to capture item', 'error');
    }
  } catch (err) {
    showToast('Offline error: Could not reach local server', 'error');
  }
}

// Data Refresh
async function loadAllData() {
  await Promise.all([
    loadTasks(),
    loadNotes(),
    loadMemories(),
    loadMetrics()
  ]);
}

async function loadMetrics() {
  try {
    const res = await fetch('/api/memory/metrics');
    const data = await res.json();
    if (data.success) {
      AppState.metrics = data.metrics;
      updateMetricsUI(data.metrics);
    }
  } catch (e) {
    console.error("Failed to load metrics", e);
  }
}

function updateMetricsUI(metrics) {
  const prec = document.getElementById('sidebarPrecision');
  const corr = document.getElementById('sidebarCorrectionRate');
  if (prec) prec.innerText = `${metrics.memory_precision_percent}%`;
  if (corr) corr.innerText = `${metrics.feedback.correction_rate_percent}%`;

  const mActive = document.getElementById('metricActiveMemories');
  const mCorr = document.getElementById('metricCorrectionRate');
  const mPrec = document.getElementById('metricPrecision');
  const mDec = document.getElementById('metricTotalDecisions');
  const mDecBreakdown = document.getElementById('metricDecisionBreakdown');

  if (mActive) mActive.innerText = metrics.active_memories;
  if (mCorr) mCorr.innerText = `${metrics.feedback.correction_rate_percent}%`;
  if (mPrec) mPrec.innerText = `${metrics.memory_precision_percent}%`;
  if (mDec) mDec.innerText = metrics.decisions.total;
  if (mDecBreakdown) {
    mDecBreakdown.innerText = `+${metrics.decisions.adds} Adds • ${metrics.decisions.updates} Updates • ${metrics.decisions.deletes} Deletes`;
  }
}
