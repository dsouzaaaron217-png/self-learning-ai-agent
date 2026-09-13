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
  viewMode: 'kanban',
  authInitialized: false,
  authenticated: false,
  csrfToken: null
};

// Global Fetch Wrapper: Automatically attaches session credentials and X-CSRF-Token
const _nativeFetch = window.fetch;
window.fetch = async function(url, options = {}) {
  options = options || {};
  options.credentials = 'same-origin';
  options.headers = options.headers || {};

  if (AppState.csrfToken) {
    if (options.headers instanceof Headers) {
      options.headers.set('X-CSRF-Token', AppState.csrfToken);
    } else {
      options.headers['X-CSRF-Token'] = AppState.csrfToken;
    }
  }

  const response = await _nativeFetch(url, options);

  // If 401 Unauthorized and not an auth endpoint, trigger auth overlay
  if (response.status === 401 && typeof url === 'string' && !url.includes('/api/auth/')) {
    AppState.authenticated = false;
    checkAuthStatus();
  }
  return response;
};

// Initialization
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initShortcuts();
  checkAuthStatus();
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

// Authentication & Session Management
async function checkAuthStatus() {
  try {
    const res = await _nativeFetch('/api/auth/status', { credentials: 'same-origin' });
    const data = await res.json();
    if (data.success) {
      AppState.authInitialized = data.initialized;
      AppState.authenticated = data.authenticated;
      AppState.csrfToken = data.csrf_token;

      const logoutBtn = document.getElementById('logoutBtn');

      if (!AppState.authInitialized) {
        showAuthModal('setup');
        if (logoutBtn) logoutBtn.style.display = 'none';
      } else if (!AppState.authenticated) {
        showAuthModal('login');
        if (logoutBtn) logoutBtn.style.display = 'none';
      } else {
        closeModal('authModal');
        if (logoutBtn) logoutBtn.style.display = 'inline-flex';
        loadAllData();
      }
    }
  } catch (err) {
    console.error('Failed to check auth status', err);
  }
}

function showAuthModal(mode) {
  const title = document.getElementById('authModalTitle');
  const notice = document.getElementById('authNotice');
  const error = document.getElementById('authErrorMsg');
  const setupFields = document.getElementById('authSetupFields');
  const loginFields = document.getElementById('authLoginFields');
  const submitBtn = document.getElementById('authSubmitBtn');

  if (error) {
    error.innerText = '';
    error.classList.add('hidden');
  }

  if (mode === 'setup') {
    if (title) title.innerText = '🔐 Cognito Setup — Create Master Password';
    if (notice) notice.innerText = 'Welcome! Cognito runs 100% locally on your machine. Please set a master password to protect your personal productivity data and API from unauthorized local access.';
    if (setupFields) setupFields.classList.remove('hidden');
    if (loginFields) loginFields.classList.add('hidden');
    if (submitBtn) submitBtn.innerText = 'Create Password & Unlock';
    setTimeout(() => {
      const p = document.getElementById('authSetupPassword');
      if (p) p.focus();
    }, 100);
  } else {
    if (title) title.innerText = '🔒 Unlock Cognito';
    if (notice) notice.innerText = 'Please enter your master password to access your local workspace.';
    if (setupFields) setupFields.classList.add('hidden');
    if (loginFields) loginFields.classList.remove('hidden');
    if (submitBtn) submitBtn.innerText = 'Unlock';
    setTimeout(() => {
      const p = document.getElementById('authLoginPassword');
      if (p) p.focus();
    }, 100);
  }

  openModal('authModal');
}

function handleAuthKeyDown(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    submitAuth();
  }
}

async function submitAuth() {
  const error = document.getElementById('authErrorMsg');
  if (error) {
    error.innerText = '';
    error.classList.add('hidden');
  }

  if (!AppState.authInitialized) {
    const password = document.getElementById('authSetupPassword').value;
    const confirm = document.getElementById('authSetupConfirm').value;

    if (!password || password.length < 8) {
      showAuthError('Password must be at least 8 characters long.');
      return;
    }
    if (password !== confirm) {
      showAuthError('Passwords do not match.');
      return;
    }

    try {
      const res = await _nativeFetch('/api/auth/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password, confirm_password: confirm })
      });
      const data = await res.json();
      if (data.success) {
        AppState.authInitialized = true;
        AppState.authenticated = true;
        AppState.csrfToken = data.csrf_token;
        closeModal('authModal');
        const logoutBtn = document.getElementById('logoutBtn');
        if (logoutBtn) logoutBtn.style.display = 'inline-flex';
        showToast('Master password created! Cognito unlocked.', 'success');
        loadAllData();
      } else {
        showAuthError(data.error || 'Setup failed.');
      }
    } catch (err) {
      showAuthError('Connection error.');
    }
  } else {
    const password = document.getElementById('authLoginPassword').value;
    if (!password) {
      showAuthError('Please enter your password.');
      return;
    }

    try {
      const res = await _nativeFetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password })
      });
      const data = await res.json();
      if (data.success) {
        AppState.authenticated = true;
        AppState.csrfToken = data.csrf_token;
        closeModal('authModal');
        const logoutBtn = document.getElementById('logoutBtn');
        if (logoutBtn) logoutBtn.style.display = 'inline-flex';
        showToast('Cognito unlocked!', 'success');
        loadAllData();
      } else {
        showAuthError(data.error || 'Invalid password.');
      }
    } catch (err) {
      showAuthError('Connection error.');
    }
  }
}

function showAuthError(msg) {
  const error = document.getElementById('authErrorMsg');
  if (error) {
    error.innerText = msg;
    error.classList.remove('hidden');
  }
}

async function handleLogout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST' });
    AppState.authenticated = false;
    showToast('Cognito locked.', 'info');
    checkAuthStatus();
  } catch (err) {
    showToast('Logout failed.', 'error');
  }
}
