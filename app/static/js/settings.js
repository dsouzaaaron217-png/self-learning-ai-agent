/**
 * SETTINGS & ENCRYPTED DATA VAULT
 */

async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    if (data.success) {
      AppState.settings = data.settings;
      
      const engineSelect = document.getElementById('settingEngineSelect');
      const ollamaGroup = document.getElementById('ollamaSettingsGroup');
      const ollamaUrl = document.getElementById('settingOllamaUrl');
      const ollamaModel = document.getElementById('settingOllamaModel');
      const engineBadge = document.getElementById('activeEngineName');

      if (engineSelect) engineSelect.value = data.settings.engine || 'local';
      if (ollamaGroup) ollamaGroup.classList.toggle('hidden', data.settings.engine !== 'ollama');
      if (ollamaUrl) ollamaUrl.value = data.settings.ollama_url || 'http://127.0.0.1:11434';
      if (ollamaModel) ollamaModel.value = data.settings.ollama_model || 'llama3.2:3b';
      
      if (engineBadge) {
        engineBadge.innerText = data.settings.engine === 'ollama' ? 'Local Ollama' : 'Local Reasoner';
      }
    }
  } catch (err) {
    console.error('Failed to load settings', err);
  }
}

function handleEngineSelectChange() {
  const engine = document.getElementById('settingEngineSelect').value;
  const ollamaGroup = document.getElementById('ollamaSettingsGroup');
  if (ollamaGroup) {
    ollamaGroup.classList.toggle('hidden', engine !== 'ollama');
  }
}

async function testOllamaConnection() {
  const msg = document.getElementById('ollamaStatusMsg');
  if (msg) msg.innerText = 'Testing local connection...';

  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    if (msg) {
      if (data.ollama_status.is_reachable) {
        msg.innerHTML = '<span style="color:var(--success)">✓ Ollama is running and accessible on localhost!</span>';
      } else {
        msg.innerHTML = '<span style="color:var(--warning)">⚠️ Ollama not detected at 127.0.0.1:11434. (Built-in engine will handle all tasks seamlessly)</span>';
      }
    }
  } catch (err) {
    if (msg) msg.innerText = 'Connection test failed';
  }
}

async function saveEngineSettings() {
  const engine = document.getElementById('settingEngineSelect').value;
  const ollamaUrl = document.getElementById('settingOllamaUrl').value.trim();
  const ollamaModel = document.getElementById('settingOllamaModel').value.trim();

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        engine: engine,
        ollama_url: ollamaUrl,
        ollama_model: ollamaModel
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast('Settings saved successfully!', 'success');
      loadSettings();
    }
  } catch (err) {
    showToast('Failed to save settings', 'error');
  }
}

// DATA VAULT BACKUP EXPORT & IMPORT (PRD §9)
function exportDataBackup() {
  window.location.href = '/api/backup/export';
  showToast('📦 Encrypted local JSON backup generated & downloaded!', 'success');
}

async function importDataBackup(e) {
  const file = e.target.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = async (event) => {
    try {
      const parsed = JSON.parse(event.target.result);
      if (!parsed.data) {
        showToast('Invalid backup file format', 'error');
        return;
      }

      if (!confirm('Restoring will overwrite current items with this backup. Continue?')) {
        return;
      }

      const res = await fetch('/api/backup/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(parsed)
      });
      const data = await res.json();
      if (data.success) {
        showToast(`✓ Restored: ${data.restored.tasks || 0} tasks, ${data.restored.notes || 0} notes, ${data.restored.memories || 0} memories!`, 'success');
        loadAllData();
      } else {
        showToast(data.error || 'Restore failed', 'error');
      }
    } catch (err) {
      showToast('Failed to parse backup JSON file', 'error');
    }
  };
  reader.readAsText(file);
}
