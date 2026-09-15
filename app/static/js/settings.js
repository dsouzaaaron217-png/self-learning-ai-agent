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

// DATA VAULT BACKUP EXPORT & IMPORT (Phase 3F)
let pendingImportBackup = null;

function openBackupExportModal() {
  const passEl = document.getElementById('backupExportPassphrase');
  const confEl = document.getElementById('backupExportConfirm');
  const errEl = document.getElementById('backupExportErrorMsg');
  if (passEl) passEl.value = '';
  if (confEl) confEl.value = '';
  if (errEl) {
    errEl.textContent = '';
    errEl.classList.add('hidden');
  }
  openModal('backupExportModal');
  if (passEl) passEl.focus();
}

function handleBackupExportKeyDown(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    submitBackupExport();
  }
}

async function submitBackupExport() {
  const passEl = document.getElementById('backupExportPassphrase');
  const confEl = document.getElementById('backupExportConfirm');
  const errEl = document.getElementById('backupExportErrorMsg');
  const submitBtn = document.getElementById('backupExportSubmitBtn');

  const passphrase = passEl ? passEl.value : '';
  const confirmPass = confEl ? confEl.value : '';

  if (!passphrase || passphrase.length < 8) {
    if (errEl) {
      errEl.textContent = 'Backup passphrase must be at least 8 characters.';
      errEl.classList.remove('hidden');
    }
    return;
  }
  if (passphrase !== confirmPass) {
    if (errEl) {
      errEl.textContent = 'Passphrases do not match.';
      errEl.classList.remove('hidden');
    }
    return;
  }

  if (errEl) errEl.classList.add('hidden');
  if (submitBtn) submitBtn.disabled = true;

  try {
    const res = await fetch('/api/backup/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ passphrase: passphrase })
    });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      if (errEl) {
        errEl.textContent = data.error || 'Export failed.';
        errEl.classList.remove('hidden');
      }
      return;
    }

    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/);
    const filename = match ? match[1] : 'cognito_backup_encrypted.enc.json';

    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    a.remove();

    if (passEl) passEl.value = '';
    if (confEl) confEl.value = '';
    closeModal('backupExportModal');
    showToast('🔒 Encrypted backup generated & downloaded!', 'success');
  } catch (err) {
    if (errEl) {
      errEl.textContent = 'Network or export error occurred.';
      errEl.classList.remove('hidden');
    }
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

function exportPlaintextBackup() {
  if (confirm('⚠️ Warning: Plaintext backups contain unencrypted personal productivity data (notes, tasks, memories). Are you sure you want to export an unencrypted backup?')) {
    window.location.href = '/api/backup/export';
    showToast('⚠️ Plaintext backup generated & downloaded.', 'warning');
  }
}

// Backward compatibility alias
function exportDataBackup() {
  openBackupExportModal();
}

function handleBackupFileSelect(e) {
  const file = e.target.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = async (event) => {
    try {
      const parsed = JSON.parse(event.target.result);
      if (parsed.format === 'cognito_backup_encrypted') {
        pendingImportBackup = parsed;
        const passEl = document.getElementById('backupImportPassphrase');
        const errEl = document.getElementById('backupImportErrorMsg');
        if (passEl) passEl.value = '';
        if (errEl) {
          errEl.textContent = '';
          errEl.classList.add('hidden');
        }
        openModal('backupImportModal');
        if (passEl) passEl.focus();
      } else if (parsed.data) {
        pendingImportBackup = null;
        if (!confirm('⚠️ Restoring from an unencrypted legacy backup will overwrite current items with this backup. Continue?')) {
          e.target.value = '';
          return;
        }
        await executeBackupImport(parsed, null);
      } else {
        showToast('Invalid backup file format.', 'error');
      }
    } catch (err) {
      showToast('Failed to parse backup JSON file.', 'error');
    }
    e.target.value = '';
  };
  reader.readAsText(file);
}

// Backward compatibility alias
function importDataBackup(e) {
  handleBackupFileSelect(e);
}

function handleBackupImportKeyDown(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    submitBackupImport();
  }
}

async function submitBackupImport() {
  const passEl = document.getElementById('backupImportPassphrase');
  const errEl = document.getElementById('backupImportErrorMsg');
  const passphrase = passEl ? passEl.value : '';

  if (!passphrase) {
    if (errEl) {
      errEl.textContent = 'Please enter the backup passphrase.';
      errEl.classList.remove('hidden');
    }
    return;
  }

  await executeBackupImport(pendingImportBackup, passphrase);
}

async function executeBackupImport(backupPayload, passphrase) {
  const errEl = document.getElementById('backupImportErrorMsg');
  const submitBtn = document.getElementById('backupImportSubmitBtn');
  if (errEl) errEl.classList.add('hidden');
  if (submitBtn) submitBtn.disabled = true;

  try {
    const body = { backup: backupPayload };
    if (passphrase) {
      body.passphrase = passphrase;
    }

    const res = await fetch('/api/backup/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    const data = await res.json();
    if (data.success) {
      closeModal('backupImportModal');
      const passEl = document.getElementById('backupImportPassphrase');
      if (passEl) passEl.value = '';
      pendingImportBackup = null;

      if (data.warning) {
        showToast(data.warning, 'warning');
      }
      showToast(`✓ Restored: ${data.restored.tasks || 0} tasks, ${data.restored.notes || 0} notes, ${data.restored.memories || 0} memories!`, 'success');
      loadAllData();
    } else {
      if (errEl && !errEl.closest('.hidden')) {
        errEl.textContent = data.error || 'Restore failed.';
        errEl.classList.remove('hidden');
      }
      showToast(data.error || 'Restore failed', 'error');
    }
  } catch (err) {
    if (errEl) {
      errEl.textContent = 'Failed to connect to server.';
      errEl.classList.remove('hidden');
    }
    showToast('Failed to execute restore', 'error');
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}
