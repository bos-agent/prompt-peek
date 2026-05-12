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
