/**
 * OFFLINE AI COPILOT CHAT
 * Grounded on local SQLite, vector memories, tasks, and notes.
 * Supports persistent history, actionability (Task/Note), and memory feedback.
 */

const chatHistory = [];
const currentChatSession = 'default';

document.addEventListener('DOMContentLoaded', () => {
  // If already authenticated or auth not enabled yet, load history
  if (typeof AppState === 'undefined' || AppState.authenticated) {
    loadChatHistory();
  }
});

function handleChatKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
}

async function loadChatHistory() {
  const container = document.getElementById('chatMessages');
  if (!container) return;

  try {
    const res = await fetch(`/api/chat/history?session_id=${encodeURIComponent(currentChatSession)}&limit=100`);
    const data = await res.json();

    if (data.success && Array.isArray(data.messages)) {
      container.innerHTML = '';
      chatHistory.length = 0;

      if (data.messages.length === 0) {
        renderDefaultChatGreeting(container);
        return;
      }

      data.messages.forEach(msg => {
        appendChatMessage(msg.role, msg.content, msg.cited_memories, null, msg.id);
        chatHistory.push({ role: msg.role, content: msg.content });
      });

      container.scrollTop = container.scrollHeight;
    }
  } catch (err) {
    console.error('Failed to load chat history', err);
  }
}

function renderDefaultChatGreeting(container) {
  container.innerHTML = `
    <div class="message assistant-message">
      <div class="message-bubble">
        <p>Hello! I am your <strong>Offline Productivity Copilot</strong>. I run 100% locally on your machine with zero cloud connectivity.</p>
        <p>You can ask me to:</p>
        <ul>
          <li><em>"What are my highest priorities today?"</em></li>
          <li><em>"What habits have you learned about my work?"</em></li>
          <li><em>"Summarize my notes or pending items"</em></li>
        </ul>
      </div>
    </div>
  `;
}

async function sendChatMessage() {
  const input = document.getElementById('chatInput');
  const text = input ? input.value.trim() : '';
  if (!text) return;

  input.value = '';
  const userMsgId = appendChatMessage('user', text);

  // Add temporary assistant placeholder
  const placeholderId = appendChatMessage('assistant', 'Thinking locally...');

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        history: chatHistory,
        session_id: currentChatSession
      })
    });
    const data = await res.json();

    // Remove placeholder
    const ph = document.getElementById(placeholderId);
    if (ph) ph.remove();

    if (data.success) {
      appendChatMessage('assistant', data.reply, data.cited_memories, data.engine, data.assistant_message_id);
      chatHistory.push({ role: 'user', content: text });
      chatHistory.push({ role: 'assistant', content: data.reply });

      const statusEl = document.getElementById('chatEngineStatus');
      if (statusEl) {
        statusEl.innerText = `Reasoning: ${data.engine || 'Local Engine'}`;
      }

      // If user chat resulted in new/updated learned memories, refresh memory metrics & badges
      if (data.learned_memories && data.learned_memories.length > 0) {
        if (typeof loadMemories === 'function') loadMemories();
        if (typeof loadMetrics === 'function') loadMetrics();
      }
    } else {
      appendChatMessage('assistant', `⚠️ Error: ${data.error || 'Failed to process message'}`);
    }
  } catch (err) {
    const ph = document.getElementById(placeholderId);
    if (ph) ph.remove();
    appendChatMessage('assistant', '⚠️ Could not connect to local reasoning engine.');
  }
}

function appendChatMessage(role, content, citations = [], engine = null, messageId = null) {
  const container = document.getElementById('chatMessages');
  if (!container) return null;

  const msgId = messageId ? `chat-msg-${messageId}` : ('msg-' + Date.now() + '-' + Math.random().toString(36).substr(2, 5));
  const msgEl = document.createElement('div');
  msgEl.className = `message ${role}-message`;
  msgEl.id = msgId;
  if (messageId) {
    msgEl.dataset.messageId = messageId;
  }

  let citationsHtml = '';
  if (citations && citations.length > 0) {
    citationsHtml = `
      <div class="chat-citation-box">
        <span style="font-size:0.7rem; color:var(--text-muted); width: 100%;">Grounded on learned memories:</span>
        ${citations.map(c => `
          <span class="chat-citation-tag" title="${escapeHtml(c.content || '')}">
            #${c.id} ${escapeHtml(c.category || 'memory')}: ${escapeHtml((c.content || '').slice(0, 32))}...
          </span>
        `).join('')}
      </div>
    `;
  }

  // Format message content
  const formatted = formatChatMessage(content);

  // Action buttons for messages
  const actionsHtml = `
    <div class="chat-actions">
      <button class="chat-action-btn" onclick="executeChatAction('task', ${messageId ? messageId : 'null'}, this)" title="Add as task">
        📋 Task
      </button>
      <button class="chat-action-btn" onclick="executeChatAction('note', ${messageId ? messageId : 'null'}, this)" title="Save as note">
        📝 Note
      </button>
    </div>
  `;

  msgEl.innerHTML = `
    <div class="message-bubble">
      ${formatted}
      ${citationsHtml}
      ${actionsHtml}
    </div>
  `;

  container.appendChild(msgEl);
  container.scrollTop = container.scrollHeight;
  return msgId;
}

async function executeChatAction(actionType, messageId, btnEl) {
  if (btnEl.disabled) return;

  // Immediate disable to prevent duplicate clicks
  btnEl.disabled = true;
  const originalText = btnEl.innerText;
  btnEl.innerText = 'Creating...';

  // Find content from enclosing message element
  const msgContainer = btnEl.closest('.message');
  let content = '';
  if (msgContainer) {
    const bubble = msgContainer.querySelector('.message-bubble');
    if (bubble) {
      // Clone bubble and remove citations/actions to get pure text
      const clone = bubble.cloneNode(true);
      const citeBox = clone.querySelector('.chat-citation-box');
      if (citeBox) citeBox.remove();
      const actBox = clone.querySelector('.chat-actions');
      if (actBox) actBox.remove();
      content = clone.innerText.trim();
    }
  }

  const endpoint = actionType === 'task' ? '/api/chat/actions/task' : '/api/chat/actions/note';
  const payload = {
    message_id: messageId || null,
    content: content
  };

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    if (data.success) {
      btnEl.classList.add('action-success');
      btnEl.innerText = actionType === 'task' ? '✓ Task Added' : '✓ Note Saved';
      if (typeof showToast === 'function') {
        showToast(actionType === 'task' ? 'Task created from chat!' : 'Note saved from chat!', 'success');
      }

      // Refresh app data so counters and tabs stay in sync
      if (actionType === 'task' && typeof loadTasks === 'function') {
        loadTasks();
      } else if (actionType === 'note' && typeof loadNotes === 'function') {
        loadNotes();
      }
    } else {
      btnEl.disabled = false;
      btnEl.innerText = originalText;
      if (typeof showToast === 'function') {
        showToast(data.error || 'Failed to create item', 'error');
      }
    }
  } catch (err) {
    btnEl.disabled = false;
    btnEl.innerText = originalText;
    if (typeof showToast === 'function') {
      showToast('Offline error: Could not reach local server', 'error');
    }
  }
}

async function clearChatHistory() {
  if (!confirm('Are you sure you want to clear your local chat history?')) {
    return;
  }

  try {
    const res = await fetch(`/api/chat/history?session_id=${encodeURIComponent(currentChatSession)}`, {
      method: 'DELETE'
    });
    const data = await res.json();

    if (data.success) {
      chatHistory.length = 0;
      const container = document.getElementById('chatMessages');
      if (container) {
        renderDefaultChatGreeting(container);
      }
      if (typeof showToast === 'function') {
        showToast('Chat history cleared.', 'info');
      }
    } else {
      if (typeof showToast === 'function') {
        showToast(data.error || 'Failed to clear history', 'error');
      }
    }
  } catch (err) {
    if (typeof showToast === 'function') {
      showToast('Offline error: Could not reach local server', 'error');
    }
  }
}

function formatChatMessage(text) {
  if (!text) return '';
  let clean = escapeHtml(text);

  // Bold **text**
  clean = clean.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  // Italic *text*
  clean = clean.replace(/\*(.*?)\*/g, '<em>$1</em>');
  // Code `code`
  clean = clean.replace(/`(.*?)`/g, '<code>$1</code>');
  // Newlines
  clean = clean.replace(/\n/g, '<br>');

  return clean;
}
