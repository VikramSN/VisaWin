// Pre-defined Security Questions
const DEFAULT_SECURITY_QUESTIONS = [
    "What was your first car?",
    "What is your favorite food?",
    "Who was your childhood hero?",
    "Where did you meet your spouse?",
    "What high school did you attend?",
    "What is your least favorite food?",
    "What is your mother's maiden name?",
    "What is your sibling's middle name?",
    "What elementary school did you attend?",
    "In what city or town was your first job?",
    "What was the first company that you worked for?",
    "What is the name of the road/street you grew up on?",
    "What was the name of your first/current/favorite pet?",
    "What is the name of the town/city where you were born?",
    "What is the name of a college you applied to but didn't attend?"
];

// Modal Helpers
function openModal(modalId) {
    const m = document.getElementById(modalId);
    if (m) m.classList.add('active');
}

function closeModal(modalId) {
    const m = document.getElementById(modalId);
    if (m) m.classList.remove('active');
}

// Copy Config Helpers
function copyApiSnippet() {
    const url = document.getElementById('serverUrl') ? document.getElementById('serverUrl').innerText : '';
    const key = document.getElementById('apiKeyVal') ? document.getElementById('apiKeyVal').innerText : '';
    const snippet = `SERVER_URL=${url.replace('/api/trigger', '')}\nSERVER_API_KEY=${key}`;
    navigator.clipboard.writeText(snippet).then(() => {
        alert("Configuration copied to clipboard!\n\n" + snippet);
    });
}

// Filter Accounts in Table
function filterAccounts() {
    const query = document.getElementById('accountSearch').value.toLowerCase();
    const rows = document.querySelectorAll('#accountsTable tbody tr.account-row');
    rows.forEach(row => {
        const text = row.innerText.toLowerCase();
        row.style.display = text.includes(query) ? '' : 'none';
    });
}

// Toggle Account Status
async function toggleAccount(username) {
    const row = document.getElementById(`row-${username}`);
    if (row && row.classList.contains('row-booked-success')) {
        alert(`Account '${username}' is already BOOKED SUCCESS! Monitoring cannot be re-enabled.`);
        return;
    }
    try {
        const res = await fetch(`/api/accounts/${encodeURIComponent(username)}/toggle`, {
            method: 'POST'
        });
        const data = await res.json();
        if (data.status === 'success') {
            if (row) {
                const wasDisabled = row.classList.contains('row-disabled');
                row.classList.toggle('row-disabled');
                const enabledEl = document.getElementById('statEnabledCount');
                const disabledEl = document.getElementById('statDisabledCount');
                if (enabledEl && disabledEl) {
                    let curEnabled = parseInt(enabledEl.innerText) || 0;
                    let curDisabled = parseInt(disabledEl.innerText) || 0;
                    if (wasDisabled) {
                        enabledEl.innerText = curEnabled + 1;
                        disabledEl.innerText = Math.max(0, curDisabled - 1);
                    } else {
                        enabledEl.innerText = Math.max(0, curEnabled - 1);
                        disabledEl.innerText = curDisabled + 1;
                    }
                }
            }
        } else {
            alert('Failed to toggle account: ' + data.message);
        }
    } catch (e) {
        alert('Network error while toggling account: ' + e);
    }
}

// Delete Account
async function deleteAccount(username) {
    if (!confirm(`Are you sure you want to delete account '${username}'?`)) return;
    try {
        const res = await fetch(`/api/accounts/${encodeURIComponent(username)}`, {
            method: 'DELETE'
        });
        const data = await res.json();
        if (data.status === 'success') {
            const row = document.getElementById(`row-${username}`);
            if (row) row.remove();
        } else {
            alert('Failed to delete: ' + data.message);
        }
    } catch (e) {
        alert('Error: ' + e);
    }
}

// Consular Same As OFC Toggle
function toggleConsularSameAsOfc() {
    const cb = document.getElementById('consularSameAsOfc');
    const customSec = document.getElementById('consularCustomSection');
    if (!cb || !customSec) return;
    if (cb.checked) {
        customSec.style.display = 'none';
    } else {
        customSec.style.display = 'block';
    }
}

// Add / Edit Account Modal Handling
function openAddAccountModal() {
    document.getElementById('modalTitle').innerText = "Add New Account";
    document.getElementById('isEditMode').value = "false";
    document.getElementById('accUsername').removeAttribute('readonly');
    document.getElementById('accountForm').reset();
    
    // Reset Consular Preferences to Same as OFC
    const sameAsOfcCb = document.getElementById('consularSameAsOfc');
    if (sameAsOfcCb) sameAsOfcCb.checked = true;
    toggleConsularSameAsOfc();
    
    // Clear security questions container
    const container = document.getElementById('securityQuestionsContainer');
    container.innerHTML = '';
    
    // Add 3 default question rows
    for (let i = 0; i < 3; i++) {
        addSecurityQuestionRow(DEFAULT_SECURITY_QUESTIONS[i] || "");
    }
    
    openModal('accountModal');
}

function editAccount(acc) {
    document.getElementById('modalTitle').innerText = `Edit Account (${acc.username})`;
    document.getElementById('isEditMode').value = "true";
    
    const uField = document.getElementById('accUsername');
    uField.value = acc.username;
    uField.setAttribute('readonly', 'true');
    
    document.getElementById('accCustomerName').value = acc.customer_name || '';
    document.getElementById('accPassword').value = acc.password || '';
    document.getElementById('accRole').value = acc.role || 'RESERVED_BOOKING';
    document.getElementById('accOfcStartDate').value = acc.ofcStartDate || '';
    document.getElementById('accOfcEndDate').value = acc.ofcEndDate || '';
    document.getElementById('accMultiPerson').checked = Boolean(acc.multiPerson);
    document.getElementById('accEnabled').checked = Boolean(acc.enabled);
    
    // Set OFC City Checkboxes
    const ofcCities = acc.ofcCities || [];
    document.querySelectorAll("input[name='ofcCity']").forEach(cb => {
        cb.checked = ofcCities.includes(cb.value);
    });
    
    // Check if Consular differs from OFC
    const consularCities = acc.consularCities || [];
    const ofcSorted = [...ofcCities].sort().join(',');
    const consularSorted = [...consularCities].sort().join(',');
    
    const hasDifferentCities = consularCities.length > 0 && consularSorted !== ofcSorted;
    const hasDifferentDates = (acc.consularStartDate && acc.consularStartDate !== acc.ofcStartDate) ||
                             (acc.consularEndDate && acc.consularEndDate !== acc.ofcEndDate);
    const isCustomConsular = hasDifferentCities || hasDifferentDates;
    
    const sameAsOfcCb = document.getElementById('consularSameAsOfc');
    if (sameAsOfcCb) {
        sameAsOfcCb.checked = !isCustomConsular;
    }
    
    // Set Consular inputs
    document.getElementById('accConsularStartDate').value = acc.consularStartDate || '';
    document.getElementById('accConsularEndDate').value = acc.consularEndDate || '';
    document.querySelectorAll("input[name='consularCity']").forEach(cb => {
        cb.checked = consularCities.includes(cb.value);
    });
    toggleConsularSameAsOfc();
    
    // Set Security Questions
    const container = document.getElementById('securityQuestionsContainer');
    container.innerHTML = '';
    const sq = acc.security_questions || {};
    const keys = Object.keys(sq);
    if (keys.length > 0) {
        keys.forEach(k => addSecurityQuestionRow(k, sq[k]));
    } else {
        for (let i = 0; i < 3; i++) {
            addSecurityQuestionRow(DEFAULT_SECURITY_QUESTIONS[i] || "");
        }
    }
    
    openModal('accountModal');
}

function addSecurityQuestionRow(selectedQuestion = "", answer = "") {
    const container = document.getElementById('securityQuestionsContainer');
    const row = document.createElement('div');
    row.className = 'sq-row';
    
    let optionsHtml = `<option value="">-- Select Question --</option>`;
    DEFAULT_SECURITY_QUESTIONS.forEach(q => {
        const isSel = q === selectedQuestion ? 'selected' : '';
        optionsHtml += `<option value="${q}" ${isSel}>${q}</option>`;
    });
    
    row.innerHTML = `
        <select class="form-control sq-question" style="flex: 1.5;">
            ${optionsHtml}
        </select>
        <input type="text" class="form-control sq-answer" placeholder="Answer" value="${answer}" style="flex: 1;">
        <button type="button" class="btn-action delete" onclick="this.parentElement.remove()" title="Remove">✕</button>
    `;
    container.appendChild(row);
}

async function handleAccountFormSubmit(event) {
    event.preventDefault();
    const isEdit = document.getElementById('isEditMode').value === "true";
    const username = document.getElementById('accUsername').value.trim();
    
    // Gather OFC cities
    const ofcCities = [];
    document.querySelectorAll("input[name='ofcCity']:checked").forEach(cb => {
        ofcCities.push(cb.value);
    });
    
    const ofcStartDate = document.getElementById('accOfcStartDate').value;
    const ofcEndDate = document.getElementById('accOfcEndDate').value;
    
    // Consular Preferences
    const sameAsOfc = document.getElementById('consularSameAsOfc') ? document.getElementById('consularSameAsOfc').checked : true;
    let consularCities = [];
    let consularStartDate = '';
    let consularEndDate = '';
    
    if (sameAsOfc) {
        consularCities = [...ofcCities];
        consularStartDate = ofcStartDate;
        consularEndDate = ofcEndDate;
    } else {
        document.querySelectorAll("input[name='consularCity']:checked").forEach(cb => {
            consularCities.push(cb.value);
        });
        consularStartDate = document.getElementById('accConsularStartDate').value;
        consularEndDate = document.getElementById('accConsularEndDate').value;
    }
    
    // Gather security questions
    const securityQuestions = {};
    document.querySelectorAll('.sq-row').forEach(row => {
        const q = row.querySelector('.sq-question').value.trim();
        const a = row.querySelector('.sq-answer').value.trim();
        if (q && a) {
            securityQuestions[q] = a;
        }
    });
    
    const payload = {
        customer_name: document.getElementById('accCustomerName').value.trim(),
        username: username,
        password: document.getElementById('accPassword').value,
        role: document.getElementById('accRole').value,
        action_mode: 'SNIPER',
        ofcCities: ofcCities,
        ofcStartDate: ofcStartDate,
        ofcEndDate: ofcEndDate,
        consularCities: consularCities,
        consularStartDate: consularStartDate,
        consularEndDate: consularEndDate,
        security_questions: securityQuestions,
        multiPerson: document.getElementById('accMultiPerson').checked,
        enabled: document.getElementById('accEnabled').checked
    };
    
    const url = isEdit ? `/api/accounts/${encodeURIComponent(username)}` : `/api/accounts`;
    const method = isEdit ? 'PUT' : 'POST';
    
    try {
        const res = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.status === 'success') {
            closeModal('accountModal');
            location.reload();
        } else {
            alert('Error saving account: ' + data.message);
        }
    } catch (e) {
        alert('Network error: ' + e);
    }
}

// Queue Tabs with Persistence
function switchQueueTab(tabId, btn) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    if (btn) {
        btn.classList.add('active');
    } else {
        const matchingBtn = document.querySelector(`[onclick*="${tabId}"]`);
        if (matchingBtn) matchingBtn.classList.add('active');
    }
    const panel = document.getElementById(tabId);
    if (panel) panel.classList.add('active');
    try {
        sessionStorage.setItem('active_queue_tab', tabId);
    } catch (e) {}
}

// Real-time table search/filter for Queue & History tabs
function filterQueueTable(tableId, query) {
    const q = (query || "").toLowerCase().trim();
    const table = document.getElementById(tableId);
    if (!table) return;
    const rows = table.querySelectorAll("tbody tr");
    rows.forEach(row => {
        if (row.querySelector(".empty-state")) return;
        const text = row.innerText.toLowerCase();
        row.style.display = text.includes(q) ? "" : "none";
    });
}

// Auto-restore active tab on load
document.addEventListener('DOMContentLoaded', () => {
    try {
        const savedTab = sessionStorage.getItem('active_queue_tab');
        if (savedTab && document.getElementById(savedTab)) {
            switchQueueTab(savedTab, null);
        }
    } catch (e) {}
});

// Update Queue Status
async function updateQueueStatus(queueId, status) {
    try {
        const res = await fetch(`/api/queue/${queueId}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: status })
        });
        const data = await res.json();
        if (data.status === 'success') {
            location.reload();
        } else {
            alert('Failed: ' + (data.message || 'Status update failed'));
        }
    } catch (e) {
        alert('Error: ' + e);
    }
}

// Delete single queue item
async function deleteQueueItem(queueId) {
    if (!confirm(`Are you sure you want to delete task #${queueId}?`)) return;
    try {
        const res = await fetch(`/api/queue/${queueId}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.status === 'success') {
            location.reload();
        } else {
            alert('Failed to delete: ' + (data.message || 'Error'));
        }
    } catch (e) {
        alert('Error: ' + e);
    }
}

// Export accounts report as CSV
async function exportAccountsCsv() {
    try {
        const rows = document.querySelectorAll('.account-row');
        if (rows.length === 0) {
            alert('No data found to download report.');
            return;
        }
        window.location.href = '/api/accounts/export';
    } catch (e) {
        alert('Error downloading report: ' + e);
    }
}

// Download Report alias
function downloadAccountsReport() {
    exportAccountsCsv();
}

// Backward-compatible alias
function exportAccountsJson() {
    exportAccountsCsv();
}

// Clean / Reset Queue (Purges all tasks from queue)
async function resetQueue() {
    if (!confirm("Are you sure you want to clean and clear all items in the booking queue? This will purge pending/stuck tasks.")) return;
    try {
        const res = await fetch('/api/queue/reset', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            location.reload();
        } else {
            alert('Failed to reset queue: ' + (data.message || 'Unknown error'));
        }
    } catch (e) {
        alert('Error resetting queue: ' + e);
    }
}

// Reset / Clear Triggers History
async function resetTriggers() {
    if (!confirm("Are you sure you want to clear all slot trigger history?")) return;
    try {
        const res = await fetch('/api/triggers/reset', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            location.reload();
        } else {
            alert('Failed to reset triggers: ' + (data.message || 'Unknown error'));
        }
    } catch (e) {
        alert('Error resetting triggers: ' + e);
    }
}

// Download Slot History Report
function downloadSlotHistoryCSV() {
    const table = document.getElementById('slot-history-table');
    const rows = table ? table.querySelectorAll('tbody tr') : [];
    const hasEmpty = table ? table.querySelector('.empty-state') : null;
    if (hasEmpty || rows.length === 0) {
        alert('No data found to download report.');
        return;
    }
    window.location.href = '/api/slots/history/export';
}

// Clear Slot History
async function clearSlotHistory() {
    if (!confirm("Are you sure you want to clear all Slot Monitoring History?")) return;
    try {
        const res = await fetch('/api/slots/history/clear', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            location.reload();
        } else {
            alert('Failed to clear slot history: ' + (data.message || 'Error'));
        }
    } catch (e) {
        alert('Error clearing slot history: ' + e);
    }
}

// Share / Copy Slot History to Clipboard
async function shareSlotHistory() {
    try {
        const res = await fetch('/api/slots/history?limit=25');
        const data = await res.json();
        if (!data.history || data.history.length === 0) {
            alert('No slots in history to share yet.');
            return;
        }
        let text = '🚨 *US VISA SLOTS MONITORING REPORT*\n';
        text += '========================================\n';
        data.history.forEach((item, idx) => {
            const dt = (item.created_at || '').substring(0, 19).replace('T', ' ');
            text += `#${idx + 1} | ${item.city} | Date: ${item.slot_date} | Qty: ${item.quantity || 1} | Time: ${dt} | Source: ${item.discovered_by || 'Monitor'}\n`;
        });
        text += '========================================\n';
        text += 'Generated via VisaBot Automated Scheduler';

        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            alert('✅ Slot Monitoring Report copied to clipboard!\nYou can now paste and share it on WhatsApp/Telegram.');
        } else {
            prompt('Copy slot report below:', text);
        }
    } catch (e) {
        alert('Could not copy report: ' + e);
    }
}

// Download OFC / Consular Bookings Report
function downloadBookingsCSV(stage) {
    const s = (stage || 'OFC').toUpperCase();
    const tableId = s === 'OFC' ? 'ofc-bookings-table' : 'consular-bookings-table';
    const table = document.getElementById(tableId);
    const rows = table ? table.querySelectorAll('tbody tr') : [];
    const hasEmpty = table ? table.querySelector('.empty-state') : null;
    if (hasEmpty || rows.length === 0) {
        alert('No data found to download report.');
        return;
    }
    window.location.href = `/api/bookings/export?stage=${encodeURIComponent(s)}`;
}

// Delete single booking record
async function deleteBookingRecord(recordId, stage) {
    const label = (stage || 'Booking').toUpperCase();
    if (!confirm(`Are you sure you want to delete ${label} record #${recordId}?`)) return;
    try {
        const res = await fetch(`/api/bookings/${recordId}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.status === 'success') {
            location.reload();
        } else {
            alert('Failed to delete: ' + (data.message || 'Unknown error'));
        }
    } catch (e) {
        alert('Error: ' + e);
    }
}

// Clear all OFC / Consular Bookings History
async function clearBookings(stage) {
    const s = (stage || 'OFC').toUpperCase();
    if (!confirm(`Are you sure you want to clear all ${s} Bookings & Attempts History? This action cannot be undone.`)) return;
    try {
        const res = await fetch(`/api/bookings/clear?stage=${encodeURIComponent(s)}`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            location.reload();
        } else {
            alert('Failed to clear history: ' + (data.message || 'Unknown error'));
        }
    } catch (e) {
        alert('Error clearing booking history: ' + e);
    }
}

// Share / Copy OFC or Consular Bookings to Clipboard
async function shareBookings(stage) {
    const s = (stage || 'OFC').toUpperCase();
    const icon = s === 'OFC' ? '🏛️' : '🛂';
    try {
        const res = await fetch(`/api/bookings?stage=${encodeURIComponent(s)}&limit=25`);
        const data = await res.json();
        if (!data.records || data.records.length === 0) {
            alert(`No ${s} booking records in history to share yet.`);
            return;
        }
        let text = `${icon} *US VISA ${s} BOOKINGS & ATTEMPTS REPORT*\n`;
        text += '========================================\n';
        data.records.forEach((b, idx) => {
            const dt = (b.created_at || '').substring(0, 19).replace('T', ' ');
            const st = b.status || 'ATTEMPTED';
            const user = b.customer_name || b.username || 'N/A';
            text += `#${idx + 1} | User: ${user} | City: ${b.city} | Slot Date: ${b.slot_date} | Status: ${st} | Time: ${dt}\n`;
            if (b.notes) {
                text += `   ↳ Note: ${b.notes}\n`;
            }
        });
        text += '========================================\n';
        text += `Generated via VisaBot Automated Scheduler (${s} Report)`;

        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            alert(`✅ ${s} Bookings Report copied to clipboard!\nYou can now paste and share it on WhatsApp/Telegram.`);
        } else {
            prompt(`Copy ${s} report below:`, text);
        }
    } catch (e) {
        alert('Could not copy report: ' + e);
    }
}


