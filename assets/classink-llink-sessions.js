(() => {
  'use strict';

  const config = window.CLASSINK_LLINK_CONFIG || {};
  const API = String(config.apiBase || 'https://class-net-p.onrender.com').replace(/\/+$/, '');
  const memory = new Map();
  const mounted = new WeakMap();

  // Render free services are contacted at startup and then every five minutes.
  const ping = () => {
    fetch(API + '/api/keepalive', {
      method: 'GET', cache: 'no-store', credentials: 'omit',
      headers: { 'Accept': 'application/json' }
    }).catch(() => {});
  };
  ping();
  window.setInterval(ping, 5 * 60 * 1000);

  const uid = () => {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return 'llink_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 14);
  };
  const currentStudent = () => {
    if (typeof config.student === 'function') return config.student();
    if (typeof window.__CLASSINK_SUIVI_LLINK_CONTEXT__ === 'function') return window.__CLASSINK_SUIVI_LLINK_CONTEXT__();
    return null;
  };
  const headersFor = app => {
    const headers = new Headers({ 'X-CRYPT-App': app, 'Accept': 'application/json' });
    if (typeof config.headers === 'function') {
      const supplied = config.headers() || {};
      Object.entries(supplied).forEach(([key, value]) => { if (value) headers.set(key, value); });
    }
    return headers;
  };
  const identityKey = (app, student) => {
    if (app === 'suivi' && student) return String(student.school_id || '') + ':' + String(student.student_code || '');
    if (typeof config.owner === 'function') return String(config.owner() || app);
    return app;
  };
  const storeKey = (app, student) => 'classink:llink:sessions:v1:' + app + ':' + identityKey(app, student);
  const readLocal = key => {
    try {
      const raw = window.localStorage.getItem(key);
      const value = raw ? JSON.parse(raw) : null;
      return Array.isArray(value) ? value : [];
    } catch (_) {
      return memory.get(key) || [];
    }
  };
  const writeLocal = (key, value) => {
    memory.set(key, value);
    try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* stockage local facultatif */ }
  };
  const studentQuery = student => {
    const query = new URLSearchParams();
    if (student && student.school_id) query.set('school_id', student.school_id);
    if (student && student.student_code) query.set('student_code', student.student_code);
    const suffix = query.toString();
    return suffix ? '?' + suffix : '';
  };
  const safeText = value => String(value == null ? '' : value);
  const cleanMessages = value => (Array.isArray(value) ? value : [])
    .filter(item => item && (item.role === 'user' || item.role === 'assistant') && typeof item.text === 'string')
    .slice(-100)
    .map(item => ({ role: item.role, text: item.text.slice(0, 12000) }));

  function mount(root) {
    if (!root) return;
    const app = String(root.dataset.llinkApp || '').toLowerCase();
    if (!['classink', 'primeink', 'manager', 'suivi'].includes(app)) return;
    const student = app === 'suivi' ? currentStudent() : null;
    const key = storeKey(app, student);
    const previous = mounted.get(root);
    if (previous && previous.key === key) return;
    if (previous && previous.cleanup) previous.cleanup();

    const messagesEl = root.querySelector('[data-llink-messages]');
    const input = root.querySelector('[data-llink-input]');
    const form = root.querySelector('[data-llink-form]');
    const send = root.querySelector('[data-llink-send]');
    const select = root.querySelector('[data-llink-sessions]');
    const newButton = root.querySelector('[data-llink-new]');
    const deleteButton = root.querySelector('[data-llink-delete]');
    const status = root.querySelector('[data-llink-status]');
    if (!messagesEl || !input || !form || !send || !select || !newButton || !deleteButton) return;

    const headers = headersFor(app);
    headers.set('Content-Type', 'application/json');
    const authHeaders = () => {
      const result = headersFor(app);
      result.set('Content-Type', 'application/json');
      return result;
    };
    let conversations = readLocal(key).map(item => ({
      id: String(item.id || uid()),
      title: String(item.title || 'Nouvelle conversation'),
      messages: cleanMessages(item.messages),
      updatedAt: item.updatedAt || new Date().toISOString()
    }));
    let activeId = conversations[0] && conversations[0].id;
    let busy = false;
    let disposed = false;

    const active = () => conversations.find(item => item.id === activeId) || null;
    const persistLocal = () => {
      conversations.sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
      writeLocal(key, conversations);
      renderSessions();
    };
    const addMessage = (role, text) => {
      const element = document.createElement('div');
      element.className = 'llink-session-message ' + role;
      element.textContent = safeText(text);
      element.style.cssText = 'max-width:92%;padding:10px 12px;border-radius:12px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.5;' +
        (role === 'user'
          ? 'align-self:flex-end;background:#315ca0;color:#fff;'
          : 'align-self:flex-start;background:var(--card-bg, var(--surface, #182235));color:var(--text-primary, var(--text, #edf2fb));border:1px solid var(--border-color, var(--line, #334155));');
      messagesEl.appendChild(element);
      messagesEl.scrollTop = messagesEl.scrollHeight;
      return element;
    };
    const renderMessages = () => {
      const item = active();
      messagesEl.replaceChildren();
      if (!item || !item.messages.length) {
        addMessage('assistant', app === 'suivi'
          ? 'Bonjour ! Je peux expliquer les informations autorisées concernant l’enfant sélectionné.'
          : 'Bonjour ! Je suis Llink. Posez votre question pour commencer une conversation.');
        return;
      }
      item.messages.forEach(message => addMessage(message.role, message.text));
      messagesEl.scrollTop = messagesEl.scrollHeight;
    };
    const renderSessions = () => {
      const item = active();
      select.replaceChildren();
      conversations.forEach(conversation => {
        const option = document.createElement('option');
        option.value = conversation.id;
        option.textContent = conversation.title || 'Nouvelle conversation';
        select.appendChild(option);
      });
      select.value = item ? item.id : '';
      deleteButton.disabled = !item;
    };
    const createConversation = () => {
      const item = { id: uid(), title: 'Nouvelle conversation', messages: [], updatedAt: new Date().toISOString() };
      conversations.unshift(item);
      activeId = item.id;
      persistLocal();
      renderMessages();
      input.focus();
    };
    const studentPayload = () => app === 'suivi' ? currentStudent() : null;
    const requestOptions = (method, body) => ({
      method, headers: authHeaders(), cache: 'no-store', credentials: 'omit',
      ...(body === undefined ? {} : { body: JSON.stringify(body) })
    });
    const saveServer = async () => {
      const item = active();
      if (!item) return;
      const payload = { title: item.title, messages: item.messages };
      const selectedStudent = studentPayload();
      if (selectedStudent) payload.student = selectedStudent;
      try {
        const response = await fetch(API + '/api/llink/conversations/' + encodeURIComponent(item.id), requestOptions('PUT', payload));
        if (!response.ok) throw new Error('HTTP ' + response.status);
        if (status) status.textContent = 'Synchronisé';
      } catch (_) {
        if (status) status.textContent = 'Sauvegardé sur cet appareil · serveur indisponible';
      }
    };
    const refreshServer = async () => {
      try {
        const response = await fetch(API + '/api/llink/conversations' + studentQuery(student), requestOptions('GET'));
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const payload = await response.json();
        const remote = Array.isArray(payload.data) ? payload.data : [];
        remote.forEach(row => {
          const normalized = {
            id: String(row.id),
            title: String(row.title || 'Nouvelle conversation'),
            messages: cleanMessages(row.messages),
            updatedAt: row.updatedAt || new Date().toISOString()
          };
          const existing = conversations.find(item => item.id === normalized.id);
          if (!existing) conversations.push(normalized);
          else if (String(normalized.updatedAt) > String(existing.updatedAt) && !busy) Object.assign(existing, normalized);
        });
        persistLocal();
        if (!activeId && conversations.length) activeId = conversations[0].id;
        renderSessions();
        renderMessages();
        if (status) status.textContent = 'Conversations synchronisées';
      } catch (_) {
        if (status) status.textContent = 'Mode appareil · synchronisation serveur indisponible';
      }
    };
    const cleanup = () => {
      disposed = true;
      form.removeEventListener('submit', onSubmit);
      select.removeEventListener('change', onSelect);
      newButton.removeEventListener('click', onNew);
      deleteButton.removeEventListener('click', onDelete);
    };
    const onSelect = () => {
      activeId = select.value;
      renderMessages();
    };
    const onNew = () => createConversation();
    const onDelete = async () => {
      const item = active();
      if (!item || busy) return;
      conversations = conversations.filter(row => row.id !== item.id);
      activeId = conversations[0] && conversations[0].id;
      persistLocal();
      renderMessages();
      const selectedStudent = studentPayload();
      const suffix = studentQuery(selectedStudent);
      try {
        await fetch(API + '/api/llink/conversations/' + encodeURIComponent(item.id) + suffix, requestOptions('DELETE'));
      } catch (_) { /* suppression locale conservée */ }
    };
    const onSubmit = async event => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text || busy) return;
      if (app === 'suivi' && !studentPayload()) {
        addMessage('assistant', 'Liez ou sélectionnez d’abord un enfant dans ClassInk Suivi.');
        return;
      }
      const item = active() || (createConversation(), active());
      if (!item) return;
      item.messages.push({ role: 'user', text });
      if (item.title === 'Nouvelle conversation') item.title = text.replace(/\s+/g, ' ').slice(0, 48) || 'Nouvelle conversation';
      item.updatedAt = new Date().toISOString();
      input.value = '';
      persistLocal();
      renderMessages();
      busy = true;
      send.disabled = true;
      input.disabled = true;
      select.disabled = true;
      newButton.disabled = true;
      deleteButton.disabled = true;
      if (status) status.textContent = 'Llink réfléchit…';
      const history = item.messages.slice(0, -1).slice(-8);
      const answerMessage = { role: 'assistant', text: 'Llink réfléchit…' };
      item.messages.push(answerMessage);
      renderMessages();
      const answerEl = messagesEl.lastElementChild;
      persistLocal();
      await saveServer();
      try {
        const requestHeaders = authHeaders();
        requestHeaders.set('Accept', 'text/event-stream');
        const selectedStudent = studentPayload();
        const response = await fetch(API + '/api/llink/assist', {
          method: 'POST', headers: requestHeaders, cache: 'no-store', credentials: 'omit',
          body: JSON.stringify({
            message: text, history, source: app, conversation_id: item.id,
            ...(selectedStudent ? { student: selectedStudent } : {})
          })
        });
        if (!response.ok) {
          const raw = await response.text();
          let detail = raw;
          try { const parsed = JSON.parse(raw); detail = parsed.detail || parsed.message || raw; } catch (_) {}
          throw Object.assign(new Error(detail || ('Erreur HTTP ' + response.status)), { status: response.status });
        }
        answerMessage.text = '';
        if (response.body && response.body.getReader) {
          const reader = response.body.getReader(), decoder = new TextDecoder('utf-8');
          while (true) {
            const part = await reader.read();
            if (part.done) break;
            answerMessage.text += decoder.decode(part.value, { stream: true });
            if (answerEl) answerEl.textContent = answerMessage.text;
            messagesEl.scrollTop = messagesEl.scrollHeight;
          }
          answerMessage.text += decoder.decode();
        } else answerMessage.text = await response.text();
        if (!answerMessage.text.trim()) answerMessage.text = 'Llink n’a pas retourné de réponse. Réessayez.';
      } catch (error) {
        answerMessage.text = error.status === 429
          ? 'Llink a atteint sa limite temporaire de requêtes. Votre message est conservé ; patientez quelques instants avant de réessayer. (' + (error.message || 'HTTP 429') + ')'
          : 'Llink est momentanément indisponible : ' + (error.message || 'erreur réseau');
      } finally {
        item.updatedAt = new Date().toISOString();
        busy = false;
        send.disabled = false;
        input.disabled = false;
        select.disabled = false;
        newButton.disabled = false;
        deleteButton.disabled = !active();
        if (answerEl) answerEl.textContent = answerMessage.text;
        persistLocal();
        await saveServer();
        if (status && errorStatus(answerMessage.text)) status.textContent = 'Réponse sauvegardée · réessayez plus tard';
        else if (status) status.textContent = 'Conversations synchronisées';
        if (!disposed) input.focus();
      }
    };
    function errorStatus(value) { return /limite temporaire/i.test(String(value || '')); }

    select.addEventListener('change', onSelect);
    newButton.addEventListener('click', onNew);
    deleteButton.addEventListener('click', onDelete);
    form.addEventListener('submit', onSubmit);
    renderSessions();
    renderMessages();
    mounted.set(root, { key, cleanup });
    refreshServer();
  }

  window.ClassInkLlink = { mount, ping };
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-llink-app]').forEach(mount);
  });
})();