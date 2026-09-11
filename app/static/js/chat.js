/**
 * OFFLINE AI COPILOT CHAT
 */

const chatHistory = [];

function handleChatKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
}

async function sendChatMessage() {
  const input = document.getElementById('chatInput');
  const text = input ? input.value.trim() : '';
  if (!text) return;

  input.value = '';
  appendChatMessage('user', text);

  // Add temporary assistant placeholder
  const placeholderId = appendChatMessage('assistant', 'Thinking locally...');

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: chatHistory })
    });
    const data = await res.json();
    
    // Remove placeholder
    const ph = document.getElementById(placeholderId);
    if (ph) ph.remove();

    if (data.success) {
      appendChatMessage('assistant', data.reply, data.cited_memories, data.engine);
      chatHistory.push({ role: 'user', content: text });
      chatHistory.push({ role: 'assistant', content: data.reply });
      
      const statusEl = document.getElementById('chatEngineStatus');
      if (statusEl) {
        statusEl.innerText = `Reasoning: ${data.engine || 'Local Engine'}`;
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

function appendChatMessage(role, content, citations = [], engine = null) {
  const container = document.getElementById('chatMessages');
  if (!container) return null;

  const msgId = 'msg-' + Date.now() + '-' + Math.random().toString(36).substr(2, 5);
  const msgEl = document.createElement('div');
  msgEl.className = `message ${role}-message`;
  msgEl.id = msgId;

  let citationsHtml = '';
  if (citations && citations.length > 0) {
    citationsHtml = `
      <div class="chat-citation-box">
        <span style="font-size:0.7rem; color:var(--text-muted); width: 100%;">Grounded on learned memories:</span>
        ${citations.map(c => `
          <span class="chat-citation-tag" title="${escapeHtml(c.content)}">
            #${c.id} ${escapeHtml(c.category)}: ${escapeHtml(c.content.slice(0, 32))}...
          </span>
        `).join('')}
      </div>
    `;
  }

  // Simple Markdown parsing for formatting
  const formatted = formatChatMessage(content);

  msgEl.innerHTML = `
    <div class="message-bubble">
      ${formatted}
      ${citationsHtml}
    </div>
  `;

  container.appendChild(msgEl);
  container.scrollTop = container.scrollHeight;
  return msgId;
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
