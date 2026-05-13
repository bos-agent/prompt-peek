/* prompt-peek shared utilities */

function escapeHTML(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

function statusClass(code) {
    if (!code) return 'status-0';
    if (code >= 200 && code < 300) return 'status-2xx';
    if (code >= 400 && code < 500) return 'status-4xx';
    if (code >= 500) return 'status-5xx';
    return 'status-0';
}

function formatSize(bytes) {
    if (!bytes) return '';
    if (bytes > 1_000_000) return (bytes / 1_000_000).toFixed(1) + ' MB';
    if (bytes > 1_000)    return (bytes / 1_000).toFixed(1) + ' KB';
    return bytes + ' B';
}

function formatDuration(ms) {
    if (!ms) return '';
    if (ms >= 1000) return (ms / 1000).toFixed(1) + 's';
    return Math.round(ms) + 'ms';
}

function formatTime(ts) {
    return new Date(ts * 1000).toLocaleString();
}

function formatJSON(obj) {
    try {
        return JSON.stringify(obj, null, 2);
    } catch (e) {
        return String(obj);
    }
}

function escapeAttr(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/'/g, '&#39;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function showToolModal(el) {
    const existing = document.querySelector('.tool-modal');
    if (existing) existing.remove();

    let tool;
    try {
        tool = JSON.parse(el.getAttribute('data-tool'));
    } catch (e) {
        return;
    }
    if (!tool) return;

    const schema = tool.schema || null;
    const required = (schema && schema.required) || [];
    const props = (schema && schema.properties) || {};

    let html = `<div class="tool-modal-header">${escapeHTML(tool.name)}</div>`;
    if (tool.desc) {
        html += `<div class="tool-modal-desc">${escapeHTML(tool.desc)}</div>`;
    }
    if (Object.keys(props).length) {
        html += '<div class="tool-modal-params">';
        for (const [pname, pinfo] of Object.entries(props)) {
            const req = required.includes(pname) ? ' <span class="param-required">required</span>' : '';
            html += `<span class="tool-modal-param">${escapeHTML(pname)}${req} <span class="param-type">${escapeHTML(pinfo.type || 'any')}</span></span>`;
        }
        html += '</div>';
    }

    const overlay = document.createElement('div');
    overlay.className = 'tool-modal-overlay';
    overlay.onclick = closeToolModal;

    const modal = document.createElement('div');
    modal.className = 'tool-modal';
    modal.innerHTML = html;
    modal.onclick = (e) => e.stopPropagation();

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    document.addEventListener('keydown', onToolModalKey);
}

function closeToolModal() {
    const overlay = document.querySelector('.tool-modal-overlay');
    if (overlay) overlay.remove();
    document.removeEventListener('keydown', onToolModalKey);
}

function onToolModalKey(e) {
    if (e.key === 'Escape') closeToolModal();
}

function showMessageModal(el) {
    const existing = document.querySelector('.tool-modal-overlay');
    if (existing) existing.remove();

    let msg;
    try {
        msg = JSON.parse(el.getAttribute('data-message'));
    } catch (e) {
        return;
    }
    if (!msg) return;

    const overlay = document.createElement('div');
    overlay.className = 'tool-modal-overlay';
    overlay.onclick = closeToolModal;

    const modal = document.createElement('div');
    modal.className = 'tool-modal';
    modal.onclick = (e) => e.stopPropagation();
    modal.innerHTML = `<div class="tool-modal-header">
        <span class="msg-role-badge role-${msg.role}">${escapeHTML(msg.role.toUpperCase())}</span>
        ${msg.type ? '<span class="msg-label-badge">' + escapeHTML(msg.type) + '</span>' : ''}
    </div>
    <div class="tool-modal-desc" style="white-space:pre-wrap;font-family:var(--font-mono);font-size:12px;max-height:60vh;overflow-y:auto;">${escapeHTML(msg.content)}</div>`;

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    document.addEventListener('keydown', onToolModalKey);
}
