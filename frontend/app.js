const fileInput = document.getElementById("fileInput");
const uploadButton = document.getElementById("uploadButton");
const selectedFile = document.getElementById("selectedFile");
const result = document.getElementById("result");
const uploadResults = document.getElementById("uploadResults");
const uploadCapacity = document.getElementById("uploadCapacity");
let remainingSlots = null;
const API_URL = "http://127.0.0.1:8000";
const progressPanel = document.getElementById("processingProgress");
const uploadProgress = document.getElementById("uploadProgress");
const currentStage = document.getElementById("currentStage");
const progressPercent = document.getElementById("progressPercent");
const documentList = document.getElementById("documentList");
const documentsStatus = document.getElementById("documentsStatus");
const refreshDocuments = document.getElementById("refreshDocuments");
const questionPanel = document.getElementById("questionPanel");
const activeDocument = document.getElementById("activeDocument");
const themeSelect = document.getElementById("themeSelect");
const processingStatuses = new Set(["Valid", "Uploading", "Extracting", "Extracted", "Chunking", "Chunked", "Embedding", "Indexing"]);
let documents = [];
let listVersion = 0;
let documentRefreshTimer = null;
let uploadInProgress = false;
let deletingDocumentId = null;
const selectedDocumentIds = new Set();
const selectAllDocuments = document.getElementById("selectAllDocuments");
const deleteSelected = document.getElementById("deleteSelected");
const viewSelected = document.getElementById("viewSelected");
const questionForm = document.getElementById("questionForm");
const questionInput = document.getElementById("questionInput");
const askButton = document.getElementById("askButton");
const retrievalStatus = document.getElementById("retrievalStatus");
const retrievalResults = document.getElementById("retrievalResults");
const answerPanel = document.getElementById("answerPanel");
const answerText = document.getElementById("answerText");
let askInProgress = false;
let currentDocumentId = null;
let retrievalVersion = 0;
let activeStatus = "Upload";

const documentsPage = document.getElementById("documentsPage");
const questionsPage = document.getElementById("questionsPage");
const documentsNav = document.getElementById("documentsNav");
const questionsNav = document.getElementById("questionsNav");
const historySidebar = document.getElementById("historySidebar");
const historyList = document.getElementById("historyList");
const historyEmpty = document.getElementById("historyEmpty");
const answerScope = document.getElementById("answerScope");
const scopeStatus = document.getElementById("scopeStatus");
const questionHistory = [];
let uploadQueue = [];
const notificationButton = document.getElementById("notificationButton");
const notificationPanel = document.getElementById("notificationPanel");
const notificationCount = document.getElementById("notificationCount");
const notificationList = document.getElementById("notificationList");
const notificationsEmpty = document.getElementById("notificationsEmpty");
const notificationToast = document.getElementById("notificationToast");
const uploadNotifications = [];
const toastQueue = [];
const activeToasts = new Map();
const TOAST_DURATION_MS = 5000;
const NOTIFICATION_STORAGE_KEY = "qds-notifications-v2";
const HISTORY_LIMIT = 100;
const notificationLabels = {success: "Success", failed: "Failed", llm: "LLM", processed: "Processed"};
const notificationOperations = {upload: "Upload", extraction: "Extraction", chunking: "Chunking",
    embedding: "Embedding", indexing: "Indexing", processing: "Processing", delete: "Delete",
    answer: "Answer generation", notification: "Notification"};
const historyMessages = {
    uploaded: "Document uploaded successfully.", deleted: "Selected documents deleted successfully.",
    processed: "Document processing completed.", processing: "Document processing is in progress.",
    size_limit: "File exceeds the maximum size of 10 MB.", page_limit: "Maximum 2 pages allowed.",
    batch_limit: "Maximum 20 documents per upload.", capacity: "The document capacity limit was reached.",
    cleanup_pending: "Document processing failed. Cleanup is pending and can be retried.",
    connection: "Could not connect to the backend. Please retry.",
    answer_ready: "Your answer is ready.", answer_failed: "Unable to generate an answer.",
    processing_failed: "Document processing failed.", action_failed: "The document operation failed. Please retry.",
    validation_failed: "Document validation failed. Check the file type and upload limits.",
    deletion_failed: "Some selected documents could not be fully deleted. Cleanup is pending. Please retry.",
    extraction_failed: "Document extraction failed.", chunking_failed: "Document chunking failed.",
    embedding_failed: "Document embedding failed.", indexing_failed: "Document indexing failed.",
    extraction_cleanup_pending: "Document extraction failed. Cleanup is pending.",
    chunking_cleanup_pending: "Document chunking failed. Cleanup is pending.",
    embedding_cleanup_pending: "Document embedding failed. Cleanup is pending.",
    indexing_cleanup_pending: "Document indexing failed. Cleanup is pending."
};
let unreadNotifications = 0;
let displayedAnswer = null;

function historyCode(kind, reason) {
    if (kind === "llm") return /unable|failed|could not/i.test(reason) ? "answer_failed" : "answer_ready";
    if (kind === "success") return /delet/i.test(reason) ? "deleted" : "uploaded";
    if (kind === "processed") return /completed|successfully processed/i.test(reason) ? "processed" : "processing";
    if (/10 MB/i.test(reason)) return "size_limit";
    if (/pages|page count/i.test(reason)) return "page_limit";
    if (/maximum of 20|maximum 20/i.test(reason)) return "batch_limit";
    if (/capacity|more documents/i.test(reason)) return "capacity";
    if (/failed to delete|deletion/i.test(reason)) return "deletion_failed";
    for (const stage of ["extraction", "chunking", "embedding", "indexing"]) {
        if (reason.toLowerCase().includes(stage)) return `${stage}_${/cleanup.*pending/i.test(reason) ? "cleanup_pending" : "failed"}`;
    }
    if (/cleanup.*pending|cleanup could not/i.test(reason)) return "cleanup_pending";
    if (/connection|connect to|network/i.test(reason)) return "connection";
    if (/processing/i.test(reason)) return "processing_failed";
    return "action_failed";
}

function failureOperation(reason, fallback = "upload") {
    for (const stage of ["extraction", "chunking", "embedding", "indexing"]) {
        if (reason.toLowerCase().includes(stage)) return stage;
    }
    return fallback;
}

function notificationRecord(input) {
    // Persist only event context. Never copy an API response, answer, retrieved
    // passage, credential, or question-history entry into notification history.
    const kind = input.kind;
    if (!Object.hasOwn(notificationLabels, kind)) return null;
    const code = Object.hasOwn(historyMessages, input.code) ? input.code : historyCode(kind, String(input.reason || ""));
    const reason = typeof input.reason === "string" && input.reason.trim() ? input.reason : historyMessages[code];
    const operation = Object.hasOwn(notificationOperations, input.operation) ? input.operation
        : kind === "llm" ? "answer" : code === "deleted" || code === "deletion_failed" ? "delete"
        : kind === "processed" ? "processing" : failureOperation(reason);
    const result = ["success", "failure"].includes(input.result) ? input.result
        : kind === "failed" || code === "answer_failed" ? "failure" : "success";
    const createdAt = Number.isFinite(input.createdAt) && !Number.isNaN(new Date(input.createdAt).getTime()) ? input.createdAt : Date.now();
    return {kind, code, createdAt, read: input.read === true, operation, result, reason,
        filename: kind !== "llm" && typeof input.filename === "string" ? input.filename : "",
        question: kind === "llm" && typeof input.question === "string" ? input.question : ""};
}

function notificationSubject(notification, preview = false) {
    if (notification.question) {
        const question = preview ? notification.question.replace(/\s+/g, " ") : notification.question;
        return `Question: ${preview && question.length > 140 ? question.slice(0, 140) + "…" : question}`;
    }
    return notification.filename || notificationLabels[notification.kind];
}

function notificationDetails(notification) {
    return `${notificationOperations[notification.operation]} · ${notification.result === "failure" ? "Failure" : "Success"}\n${notification.reason}`;
}

function restoreNotifications() {
    try {
        const saved = JSON.parse(localStorage.getItem(NOTIFICATION_STORAGE_KEY) || "[]");
        if (Array.isArray(saved)) for (const item of saved.slice(-HISTORY_LIMIT)) {
            if (!item || typeof item !== "object" || !Number.isFinite(item.createdAt)) continue;
            const notification = notificationRecord(item);
            if (notification) uploadNotifications.push(notification);
        }
        // Discard the earlier session payload, which could contain answer text.
        sessionStorage.removeItem("qds-notifications");
    } catch { /* Keep in-memory notifications if storage is unavailable. */ }
}
restoreNotifications();

function renderNotifications() {
    notificationList.replaceChildren();
    for (const notification of [...uploadNotifications].reverse()) {
        const row = document.createElement("li");
        row.className = notification.kind;
        const icon = document.createElement("span");
        icon.className = "notification-icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = {success: "✓", failed: "✕", llm: "✦", processed: "✓"}[notification.kind];
        const content = document.createElement("button");
        content.type = "button";
        content.className = "notification-content";
        content.setAttribute("aria-label", `${notification.read ? "Read" : "Mark as read"}: ${notificationLabels[notification.kind]} · ${notificationSubject(notification)}`);
        content.addEventListener("click", () => { notification.read = true; renderNotifications(); });
        const title = document.createElement("strong");
        title.className = "notification-title";
        title.textContent = `${notificationLabels[notification.kind]} · ${notificationSubject(notification)}`;
        const reason = document.createElement("small");
        reason.className = "notification-reason";
        reason.textContent = notificationDetails(notification);
        const timestamp = document.createElement("time");
        timestamp.className = "notification-time";
        timestamp.setAttribute("datetime", new Date(notification.createdAt).toISOString());
        timestamp.textContent = new Date(notification.createdAt).toLocaleString();
        content.append(title, reason, timestamp);
        content.setAttribute("aria-pressed", String(notification.read));
        row.append(icon, content);
        notificationList.append(row);
    }
    notificationsEmpty.hidden = uploadNotifications.length > 0;
    unreadNotifications = uploadNotifications.filter(item => !item.read).length;
    notificationCount.textContent = String(unreadNotifications);
    notificationCount.hidden = unreadNotifications === 0;
    notificationButton.setAttribute("aria-label", `Notifications, ${unreadNotifications} unread`);
    // The same allowlist is applied on write and restore; extra API fields are
    // discarded even when older or externally modified storage contains them.
    try { localStorage.setItem(NOTIFICATION_STORAGE_KEY, JSON.stringify(uploadNotifications.map(notificationRecord))); }
    catch { /* In-memory history remains available. */ }
}

function dismissToast(notification) {
    const toast = activeToasts.get(notification);
    if (!toast) return;
    clearTimeout(toast.timer);
    clearTimeout(toast.fadeTimer);
    if (toast.row.contains?.(document.activeElement)) notificationButton.focus?.();
    toast.row.remove();
    activeToasts.delete(notification);
    pumpToasts();
}

function pumpToasts() {
    while (activeToasts.size < 3 && toastQueue.length) {
        const notification = toastQueue.shift();
        const row = document.createElement("div");
        row.className = `notification-toast ${notification.kind}`;
        const text = document.createElement("p");
        text.setAttribute("role", notification.kind === "failed" ? "alert" : "status");
        text.setAttribute("aria-atomic", "true");
        const close = document.createElement("button");
        close.type = "button";
        close.className = "quiet";
        close.textContent = "×";
        close.setAttribute("aria-label", `Close notification: ${notificationLabels[notification.kind]}`);
        close.addEventListener("click", () => dismissToast(notification));
        row.append(text, close);
        notificationToast.hidden = false;
        notificationToast.append(row);
        text.textContent = `${notificationLabels[notification.kind]}: ${notificationSubject(notification, true)}\n${notificationDetails(notification)}`;
        activeToasts.set(notification, {row,
            fadeTimer: setTimeout(() => { row.className += " closing"; }, TOAST_DURATION_MS - 180),
            timer: setTimeout(() => dismissToast(notification), TOAST_DURATION_MS)});
    }
    notificationToast.hidden = activeToasts.size === 0;
}

function clearToasts() {
    for (const toast of activeToasts.values()) { clearTimeout(toast.timer); clearTimeout(toast.fadeTimer); }
    activeToasts.clear(); toastQueue.length = 0;
    notificationToast.replaceChildren(); notificationToast.hidden = true;
}

function addNotification(input) {
    const notification = notificationRecord({...input, createdAt: Date.now(), read: false});
    if (!notification) throw new Error("Unknown notification category");
    uploadNotifications.push(notification);
    if (uploadNotifications.length > HISTORY_LIMIT) uploadNotifications.splice(0, uploadNotifications.length - HISTORY_LIMIT);
    toastQueue.push(notification);
    pumpToasts();
    renderNotifications();
}

function markVisibleHistoryRead() {
    if (!notificationPanel.hidden && document.visibilityState !== "hidden") {
        uploadNotifications.forEach(item => { item.read = true; });
        renderNotifications();
    }
}
document.addEventListener("visibilitychange", markVisibleHistoryRead);
function closeNotificationPanel() {
    notificationPanel.hidden = true;
    notificationButton.setAttribute("aria-expanded", "false");
    notificationButton.focus?.();
}
notificationButton.addEventListener("click", () => {
    notificationPanel.hidden = !notificationPanel.hidden;
    notificationButton.setAttribute("aria-expanded", String(!notificationPanel.hidden));
    markVisibleHistoryRead();
    if (!notificationPanel.hidden) document.getElementById("closeNotifications").focus?.();
});
document.getElementById("closeNotifications").addEventListener("click", closeNotificationPanel);
notificationPanel.addEventListener("keydown", event => { if (event.key === "Escape") closeNotificationPanel(); });
document.getElementById("clearNotifications").addEventListener("click", () => {
    uploadNotifications.length = 0; clearToasts(); renderNotifications();
});
renderNotifications();

function finishUploadEntry(entry) {
    const success = entry.status === "Ready";
    addNotification({kind: success ? "processed" : "failed", filename: entry.file.name,
        operation: success ? "processing" : failureOperation(entry.message || "", entry.processingOperation || "upload"),
        result: success ? "success" : "failure",
        reason: success ? "Extraction, chunking, and indexing completed; processing completed successfully." : entry.message});
    uploadQueue = uploadQueue.filter(item => item !== entry);
    renderUploadQueue();
    renderNotifications();
}

const uploadStages = ["Validating", "Uploading", "Extracting", "Chunking", "Embedding", "Indexing"];

function applyUploadStage(entry, event) {
    const stage = event.status === "Valid" ? "Uploading" : event.status;
    const operation = {Uploading: "upload", Extracting: "extraction", Chunking: "chunking", Embedding: "embedding", Indexing: "indexing"}[stage];
    if (operation) entry.processingOperation = operation;
    // Extracting is emitted after the original upload has succeeded. Report
    // that once; Ready later reports the separate processing completion event.
    if (stage === "Extracting" && !entry.uploadNotified) {
        entry.uploadNotified = true;
        addNotification({kind: "success", filename: entry.file.name, operation: "upload", result: "success", reason: "Uploaded successfully."});
    }
    const index = uploadStages.indexOf(stage);
    if (index >= 0) {
        entry.step = index;
        entry.status = "Processing";
        entry.message = `${stage}…`;
    } else if (stage === "Ready") {
        entry.step = uploadStages.length;
        entry.status = "Ready";
        entry.message = "Ready — uploaded successfully";
    } else if (["Failed", "Invalid"].includes(stage)) {
        entry.status = stage;
        entry.message = `${stage === "Invalid" ? "Rejected" : "Failed"} — ${event.message || "Processing did not finish."}`;
    }
    renderUploadQueue();
}

async function readUploadResponse(response, entry) {
    if (!response.ok || !(response.headers.get("content-type") || "").includes("application/x-ndjson")) {
        return response.json().catch(() => null);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", finalResult = null;
    const consume = line => {
        if (!line.trim()) return;
        const event = JSON.parse(line);
        if (event.type === "complete") finalResult = event;
        else applyUploadStage(entry, event);
    };
    try {
        while (true) {
            const {value, done} = await reader.read();
            buffer += done ? decoder.decode() : decoder.decode(value, {stream: true});
            const lines = buffer.split("\n");
            buffer = lines.pop();
            for (const line of lines) consume(line);
            if (done) break;
        }
        if (buffer.trim()) consume(buffer);
        if (!finalResult) throw new Error("Progress connection ended early. Refresh the document list before retrying.");
        return finalResult;
    } finally {
        reader.releaseLock();
    }
}

function showPage(page) {
    document.getElementById("mainContent").scrollTop = 0;
    documentsPage.hidden = page !== "documents";
    questionsPage.hidden = page !== "questions";
    historySidebar.hidden = page !== "questions";
    documentsNav.setAttribute("aria-current", page === "documents" ? "page" : "false");
    questionsNav.setAttribute("aria-current", page === "questions" ? "page" : "false");
}
documentsNav.addEventListener("click", () => showPage("documents"));
questionsNav.addEventListener("click", () => showPage("questions"));
document.getElementById("newQuestion").addEventListener("click", () => {
    resetRetrieval();
    renderScopes();
    showPage("questions");
});

function renderScopes() {
    answerScope.replaceChildren();
    const all = document.createElement("option");
    all.value = "all";
    all.textContent = "All Ready documents";
    answerScope.append(all);
    for (const item of documents.filter(item => item.status === "Ready")) {
        const option = document.createElement("option");
        option.value = item.document_id;
        option.textContent = item.file_name || item.document_id;
        answerScope.append(option);
    }
    answerScope.value = currentDocumentId || "all";
    scopeStatus.textContent = currentDocumentId ? "Answers use the selected document only."
        : "Answers search all Ready documents you have uploaded.";
}
answerScope.addEventListener("change", () => {
    const item = documents.find(item => item.document_id === answerScope.value);
    if (item) selectDocument(item);
    else { resetRetrieval(); renderScopes(); }
});

function openHistoryEntry(entry) {
    resetRetrieval();
    currentDocumentId = documents.some(item => item.document_id === entry.document_id && item.status === "Ready") ? entry.document_id : null;
    renderScopes();
    questionInput.value = entry.question;
    activeDocument.textContent = `Saved answer · ${entry.file_name}`;
    renderAnswer(entry);
    displayedAnswer = entry;
    retrievalStatus.textContent = "Saved answer from this browser session.";
    showPage("questions");
}

function renderHistory() {
    historyList.replaceChildren();
    historyEmpty.hidden = questionHistory.length > 0;
    for (const entry of [...questionHistory].reverse()) {
        const row = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "history-entry";
        const title = document.createElement("span");
        title.textContent = entry.question;
        const scope = document.createElement("small");
        scope.textContent = entry.file_name;
        button.append(title, scope);
        button.addEventListener("click", () => openHistoryEntry(entry));
        row.append(button);
        historyList.append(row);
    }
}

function renderUploadQueue() {
    uploadResults.replaceChildren();
    for (const entry of uploadQueue) {
        const row = document.createElement("li");
        const name = document.createElement("strong");
        name.textContent = entry.file.name;
        const status = document.createElement("span");
        status.textContent = entry.message;
        status.className = ["Invalid", "Failed", "Not sent"].includes(entry.status) ? "failed" : "";
        const progress = document.createElement("progress");
        progress.max = 100;
        progress.setAttribute("aria-label", `Processing ${entry.file.name}`);
        if (entry.status === "Ready") progress.value = 100;
        else if (Number.isInteger(entry.step)) progress.value = Math.round(entry.step / uploadStages.length * 100);
        else if (entry.status !== "Processing") progress.value = 0;
        row.append(name, status, progress);
        const steps = document.createElement("ol");
        steps.className = "upload-stages";
        uploadStages.forEach((stage, index) => {
            const step = document.createElement("li");
            const finished = entry.status === "Ready" || index < (entry.step ?? -1);
            const active = entry.status === "Processing" && index === entry.step;
            const failed = ["Invalid", "Failed"].includes(entry.status) && index === (entry.step ?? 0);
            step.className = finished ? "done" : failed ? "failed" : active ? "active" : "pending";
            step.textContent = `${finished ? "✓" : failed ? "✕" : active ? "⟳" : "○"} ${stage}`;
            if (active) step.setAttribute("aria-current", "step");
            steps.append(step);
        });
        row.append(steps);
        const completion = document.createElement("small");
        const completedSteps = entry.status === "Ready" ? uploadStages.length : (entry.step ?? 0);
        completion.textContent = `${Math.round(completedSteps / uploadStages.length * 100)}% · ${completedSteps} of ${uploadStages.length} steps complete`;
        row.append(completion);
        uploadResults.append(row);
    }
}

function applyTheme(value) {
    const theme = ["light", "dark", "auto"].includes(value) ? value : "auto";
    document.documentElement.dataset.theme = theme;
    themeSelect.value = theme;
    try { localStorage.setItem("qds-theme", theme); } catch { /* Storage can be disabled. */ }
}
let savedTheme = "auto";
try { savedTheme = localStorage.getItem("qds-theme") || "auto"; } catch { /* Use Auto. */ }
applyTheme(savedTheme);
themeSelect.addEventListener("change", () => applyTheme(themeSelect.value));

function resetRetrieval() {
    displayedAnswer = null;
    currentDocumentId = null;
    retrievalVersion += 1;
    questionPanel.hidden = false;
    activeDocument.textContent = "Answering from all Ready documents.";
    questionInput.value = "";
    questionInput.disabled = false;
    askButton.disabled = false;
    askInProgress = false;
    askButton.textContent = "Ask";
    questionForm.setAttribute("aria-busy", "false");
    answerPanel.hidden = true;
    answerText.textContent = "";
    retrievalStatus.textContent = "";
    retrievalResults.replaceChildren();
}

function showProgress(status, message, explicitProgress) {
    const percentages = { Upload: 15, Valid: 15, Extracting: 30, Extracted: 50,
        Chunking: 50, Chunked: 70, Embedding: 70, Indexing: 90, Ready: 100 };
    activeStatus = status;
    progressPanel.hidden = false;
    progressPanel.className = "progress-panel";
    const percent = status === "Ready" ? 100 : (typeof explicitProgress === "number" && Number.isFinite(explicitProgress)
        ? Math.max(0, Math.min(100, explicitProgress)) : percentages[status]);
    uploadProgress.value = percent;
    uploadProgress.textContent = `${percent}%`;
    progressPercent.textContent = `${percent}%`;
    currentStage.textContent = status === "Valid" ? "Upload / Validation" : status;
    result.textContent = message;
}

function showUploadError(message, stage) {
    progressPanel.className = "progress-panel failed";
    currentStage.textContent = `${stage || activeStatus} — Failed`;
    result.textContent = message;
}

function selectDocument(item) {
    if (item.status !== "Ready") return;
    resetRetrieval();
    currentDocumentId = item.document_id;
    activeDocument.textContent = item.file_name || item.document_id;
    questionPanel.hidden = item.status !== "Ready";
    renderDocuments();
    renderScopes();
    showPage("questions");
}

function renderDocuments() {
    const presentIds = new Set(documents.map(item => item.document_id));
    for (const id of selectedDocumentIds) if (!presentIds.has(id)) selectedDocumentIds.delete(id);
    documentList.replaceChildren();
    for (const item of documents) {
        const row = document.createElement("li");
        row.className = "document-item" + (item.document_id === currentDocumentId ? " selected" : "");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.className = "document-checkbox";
        checkbox.checked = selectedDocumentIds.has(item.document_id);
        checkbox.disabled = uploadInProgress || !!deletingDocumentId;
        checkbox.setAttribute("aria-label", `Select ${item.file_name || item.document_id}`);
        checkbox.addEventListener("change", () => {
            if (checkbox.checked) selectedDocumentIds.add(item.document_id);
            else selectedDocumentIds.delete(item.document_id);
            updateBulkActions();
        });
        const select = document.createElement("button");
        select.type = "button";
        select.className = "document-select";
        select.disabled = uploadInProgress || !!deletingDocumentId || item.status !== "Ready";
        select.setAttribute("aria-pressed", String(item.document_id === currentDocumentId));
        const name = document.createElement("span");
        name.textContent = item.file_name || "Untitled document";
        const status = document.createElement("span");
        status.className = "document-status";
        status.textContent = item.status || "Unknown status";
        select.append(name, status);
        select.addEventListener("click", () => selectDocument(item));
        row.append(checkbox, select);
        documentList.append(row);
    }
    updateBulkActions();
}

function updateBulkActions() {
    const busy = uploadInProgress || !!deletingDocumentId;
    selectAllDocuments.checked = documents.length > 0 && selectedDocumentIds.size === documents.length;
    selectAllDocuments.indeterminate = selectedDocumentIds.size > 0 && selectedDocumentIds.size < documents.length;
    selectAllDocuments.disabled = busy || documents.length === 0;
    deleteSelected.disabled = busy || selectedDocumentIds.size === 0;
    viewSelected.disabled = busy || selectedDocumentIds.size === 0;
    document.getElementById("selectionCount").textContent = `${selectedDocumentIds.size} document${selectedDocumentIds.size === 1 ? "" : "s"} selected`;
}
selectAllDocuments.addEventListener("change", () => {
    selectedDocumentIds.clear();
    if (selectAllDocuments.checked) documents.forEach(item => selectedDocumentIds.add(item.document_id));
    renderDocuments();
});
deleteSelected.addEventListener("click", () => deleteDocumentGroup([...selectedDocumentIds]));

const documentViewer = document.getElementById("documentViewer");
const viewerDocument = document.getElementById("viewerDocument");
const viewerStatus = document.getElementById("viewerStatus");
const viewerFrame = document.getElementById("viewerFrame");
let viewerIds = [];
let viewerVersion = 0;
let viewerObjectUrl = null;
function releaseViewerPreview() {
    viewerVersion += 1;
    viewerFrame.hidden = true;
    viewerFrame.removeAttribute("src");
    if (viewerObjectUrl) URL.revokeObjectURL(viewerObjectUrl);
    viewerObjectUrl = null;
}
function closeViewer() {
    releaseViewerPreview(); viewerIds = [];
    if (documentViewer.open) documentViewer.close();
    viewSelected.focus?.();
}
document.getElementById("closeViewer").addEventListener("click", closeViewer);
documentViewer.addEventListener("close", () => { releaseViewerPreview(); viewerIds = []; });
async function showViewerDocument() {
    releaseViewerPreview();
    const version = viewerVersion;
    const id = viewerDocument.value;
    const item = documents.find(item => item.document_id === id);
    if (!item || !viewerIds.includes(id)) return;
    if (item.original_available === false) {
        viewerStatus.textContent = "The original file is unavailable."; return;
    }
    if (!/\.pdf$/i.test(item.file_name || "")) {
        viewerStatus.textContent = "This file type cannot be previewed here. DOCX preview is not supported by the browser. No file was downloaded."; return;
    }
    viewerStatus.textContent = "Loading document…";
    try {
        const response = await fetch(`${API_URL}/documents/${encodeURIComponent(id)}/view`, {cache: "no-store"});
        if (!response.ok) {
            const data = await response.json().catch(() => null);
            throw new Error(typeof data?.detail === "string" ? data.detail : "Could not open the original document.");
        }
        const blob = await response.blob();
        if (version !== viewerVersion || !documentViewer.open) return;
        if (!(response.headers.get("content-type") || blob.type || "").includes("application/pdf")) {
            viewerStatus.textContent = "This response cannot be previewed as a PDF."; return;
        }
        viewerObjectUrl = URL.createObjectURL(blob);
        viewerFrame.src = viewerObjectUrl;
        viewerFrame.title = `Preview: ${item.file_name}`;
        viewerFrame.hidden = false;
        viewerStatus.textContent = "Original PDF preview. Use the document selector to switch files.";
    } catch (error) {
        if (version === viewerVersion) viewerStatus.textContent = error.message || "Could not open the document.";
    }
}
viewSelected.addEventListener("click", async () => {
    viewerIds = documents.filter(item => selectedDocumentIds.has(item.document_id)).map(item => item.document_id);
    if (!viewerIds.length || viewSelected.disabled) return;
    viewerDocument.replaceChildren();
    for (const id of viewerIds) {
        const item = documents.find(item => item.document_id === id);
        const option = document.createElement("option"); option.value = id; option.textContent = item.file_name || id;
        viewerDocument.append(option);
    }
    viewerDocument.value = viewerIds[0];
    documentViewer.showModal();
    await showViewerDocument();
});
viewerDocument.addEventListener("change", showViewerDocument);

function confirmDeletion(count) {
    const dialog = document.getElementById("deleteConfirmation");
    document.getElementById("deleteConfirmationText").textContent = `Are you sure you want to permanently delete ${count} selected document${count === 1 ? "" : "s"} and their associated data?`;
    return new Promise(resolve => {
        dialog.returnValue = "";
        dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true});
        dialog.showModal();
    });
}

async function loadDocuments() {
    clearTimeout(documentRefreshTimer);
    const version = ++listVersion;
    refreshDocuments.disabled = true;
    documentsStatus.textContent = "Loading documents…";
    try {
        const [response, capacityResponse] = await Promise.all([
            fetch(`${API_URL}/documents`, { cache: "no-store" }),
            fetch(`${API_URL}/documents/capacity`, { cache: "no-store" }).catch(() => null),
        ]);
        const data = await response.json();
        const capacity = capacityResponse ? await capacityResponse.json().catch(() => null) : null;
        if (version !== listVersion) return;
        if (capacityResponse?.ok && Number.isInteger(capacity?.remaining_slots)) {
            remainingSlots = capacity.remaining_slots;
            uploadCapacity.textContent = `Documents: ${capacity.current_count} / 20. Remaining capacity: ${remainingSlots}.`;
        } else {
            remainingSlots = null;
            uploadCapacity.textContent = "Capacity unavailable. The backend will check before accepting uploads.";
        }
        if (!response.ok || !Array.isArray(data)) throw new Error(typeof data.detail === "string" ? data.detail : "Could not load documents.");
        documents = data.filter(item => !["Invalid", "Failed", "FAILED_CLEANUP", "PENDING_CLEANUP", "Deleted"].includes(item.status));
        if (currentDocumentId && !documents.some(item => item.document_id === currentDocumentId && item.status === "Ready")) resetRetrieval();
        renderDocuments();
        renderScopes();
        documentsStatus.textContent = capacityResponse?.ok && Number.isInteger(capacity?.current_count)
            ? `Documents: ${capacity.current_count} / 20`
            : (documents.length ? `${documents.length} documents` : "No documents yet. Upload one to get started.");
        return true;
    } catch (error) {
        if (version === listVersion) documentsStatus.textContent = error.message || "Could not connect to the backend.";
        return false;
    } finally {
        if (version === listVersion) {
            refreshDocuments.disabled = false;
            if (documents.some(item => processingStatuses.has(item.status))) {
                documentRefreshTimer = setTimeout(() => { void loadDocuments(); }, 2000);
            }
        }
    }
}

async function deleteDocumentGroup(ids) {
    if (uploadInProgress || deletingDocumentId || !Array.isArray(ids) || !ids.length) return;
    // Snapshot IDs before opening the dialog. Select All never becomes a backend
    // wildcard, even if another upload completes while confirmation is open.
    ids = [...ids];
    const filenames = new Map(ids.map(id => [id, documents.find(item => item.document_id === id)?.file_name || id]));
    if (!await confirmDeletion(ids.length)) return;
    if (uploadInProgress || deletingDocumentId) return;
    deletingDocumentId = "bulk";
    uploadButton.disabled = true;
    fileInput.disabled = true;
    askButton.disabled = true;
    renderDocuments();
    try {
        const response = await fetch(`${API_URL}/documents/delete-batch`, {method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({document_ids: ids})});
        const data = await response.json().catch(() => null);
        if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : "Document deletion failed. Please retry.");
        const outcomes = data?.results;
        if (!Array.isArray(outcomes) || outcomes.some(item => !item || typeof item.document_id !== "string" || !["Deleted", "Failed"].includes(item.status))
                || (ids !== null && (outcomes.length !== ids.length || ids.some(id => !outcomes.some(item => item.document_id === id))))) {
            throw new Error("Deletion result is unknown. Refresh the list before retrying.");
        }
        const removed = new Set(outcomes.filter(item => item.status === "Deleted").map(item => item.document_id));
        for (const id of removed) selectedDocumentIds.delete(id);
        documents = documents.filter(item => !removed.has(item.document_id));
        if (viewerIds.some(id => removed.has(id))) closeViewer();
        if (removed.has(currentDocumentId)) resetRetrieval();
        for (const item of outcomes) {
            addNotification({kind: item.status === "Deleted" ? "success" : "failed", filename: filenames.get(item.document_id),
                operation: "delete", result: item.status === "Deleted" ? "success" : "failure",
                reason: item.status === "Deleted" ? "Deleted successfully." : item.message || "Document cleanup failed. Please retry."});
        }
    } catch (error) {
        for (const id of ids) addNotification({kind: "failed", filename: filenames.get(id), operation: "delete", result: "failure",
            reason: error.message || "Document deletion failed. Please retry."});
    } finally {
        const refreshed = await loadDocuments();
        if (refreshed === false) addNotification({kind: "failed", operation: "notification", reason: "Could not refresh the document list or count. Use Refresh to check the current state."});
        deletingDocumentId = null;
        uploadButton.disabled = false;
        fileInput.disabled = false;
        askButton.disabled = askInProgress;
        renderDocuments();
    }
}
refreshDocuments.addEventListener("click", loadDocuments);

function showUpdate(data) {
    if (!data || typeof data !== "object") throw new Error("Invalid backend response.");
    if (data.status === "Invalid" || data.status === "Failed" || data.error_reason) {
        showUploadError(data.error_reason || data.message || "Document processing failed.", data.status === "Invalid" ? "Validation" : data.failed_stage);
        return true;
    }
    const statuses = ["Upload", "Valid", "Extracting", "Extracted", "Chunking", "Chunked", "Embedding", "Indexing", "Ready"];
    if (!statuses.includes(data.status)) throw new Error("Unexpected backend response status.");
    showProgress(data.status, data.message || `Document ${data.status}`, data.progress);
    if (data.status === "Ready") {
        if (typeof data.document_id === "string" && data.document_id.trim()) {
            selectDocument({document_id: data.document_id, file_name: data.file_name || fileInput.files[0]?.name, status: "Ready"});
        }
        return true;
    }
    return false;
}

fileInput.addEventListener("change", () => {
    resetRetrieval();
    const count = fileInput.files.length;
    selectedFile.textContent = count ? `Selected documents: ${count}` : "";
    uploadQueue = Array.from(fileInput.files, file => ({file, status: "Waiting", message: "Waiting"}));
    renderUploadQueue();
    progressPanel.hidden = true;
    result.textContent = count > 20 ? "You can select a maximum of 20 documents." : "";
    renderDocuments();
});

uploadButton.addEventListener("click", async () => {
    if (uploadInProgress || deletingDocumentId) return;
    const files = Array.from(fileInput.files);
    if (!files.length) {
        result.textContent = "Please choose at least one document.";
        addNotification({kind: "failed", operation: "upload", reason: result.textContent});
        return;
    }
    if (files.length > 20) {
        result.textContent = "You can select a maximum of 20 documents.";
        for (const file of files) addNotification({kind: "failed", filename: file.name, operation: "upload", reason: result.textContent});
        return;
    }
    if (remainingSlots !== null && files.length > remainingSlots) {
        result.textContent = `You can upload only ${remainingSlots} more documents. Refresh to check current capacity.`;
        for (const file of files) addNotification({kind: "failed", filename: file.name, operation: "upload", reason: result.textContent});
        return;
    }
    resetRetrieval();
    renderScopes();
    uploadQueue = files.map(file => ({file, status: "Waiting", message: "Waiting"}));
    const batchEntries = [...uploadQueue];
    renderUploadQueue();
    progressPanel.hidden = true;
    uploadButton.disabled = true;
    fileInput.disabled = true;
    uploadInProgress = true;

    renderDocuments();
    let succeeded = 0;
    try {
        for (let index = 0; index < batchEntries.length; index += 1) {
            const entry = batchEntries[index];
            entry.status = "Processing";
            entry.step = 0;
            entry.message = "Validating and sending document…";
            renderUploadQueue();
            result.textContent = `Processing document ${index + 1} of ${files.length}…`;
            const formData = new FormData();
            formData.append("files", entry.file);
            try {
                const response = await fetch(`${API_URL}/upload?progress=true`, {method: "POST", body: formData});
                const data = await readUploadResponse(response, entry);
                if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `Upload failed (HTTP ${response.status}).`);
                if (!data || !Array.isArray(data.results) || data.results.length !== 1) throw new Error("Upload result is unknown. Refresh the document list before retrying.");
                const item = data.results[0];
                entry.status = item.status;
                entry.message = item.status === "Ready" ? "Ready — uploaded successfully"
                    : `${item.status === "Invalid" ? "Rejected" : "Failed"} — ${item.message || "Processing did not finish."}`;
                if (item.cleanup_pending) entry.message += " Cleanup pending; automatic retry is scheduled.";
                if (item.status === "Ready") succeeded += 1;
            } catch (error) {
                entry.status = "Failed";
                entry.message = error instanceof TypeError ? "Connection lost. Refresh to check whether this document finished before retrying." : error.message;
                finishUploadEntry(entry);
                for (const waiting of batchEntries.slice(index + 1)) {
                    waiting.status = "Not sent";
                    waiting.message = "Not sent — refresh capacity and try again.";
                    finishUploadEntry(waiting);
                }
                break;
            }
            finishUploadEntry(entry);
            await loadDocuments();
        }
        result.textContent = `${succeeded} of ${files.length} documents uploaded successfully. Check the notification bell for details.`;
    } finally {
        uploadInProgress = false;
        uploadButton.disabled = false;
        fileInput.disabled = false;
        fileInput.value = "";
        selectedFile.textContent = "";
        await loadDocuments();
    }
});

function renderAnswer(data) {
    retrievalResults.replaceChildren();
        answerText.textContent = data.answer;
        answerPanel.hidden = false;
        data.results.forEach((chunk, index) => {
            const item = document.createElement("li");
            const metadata = document.createElement("strong");
            const score = typeof chunk.score === "number" ? chunk.score.toFixed(4) : "—";
            const pages = Array.isArray(chunk.page_numbers) && chunk.page_numbers.length ? chunk.page_numbers.join(", ") : "—";
            metadata.textContent = `Rank ${index + 1} | Document: ${chunk.file_name || "Unknown document"} | Score: ${score} | Page: ${pages} | Section: ${chunk.section_heading || "—"}`;
            const preview = document.createElement("p");
            const content = typeof chunk.content === "string" ? chunk.content.trim() : "";
            preview.textContent = content.slice(0, 500) + (content.length > 500 ? "…" : "");
            item.append(metadata, preview);
            retrievalResults.append(item);
        });
}

questionForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (askInProgress) return;
    const question = questionInput.value.trim();
    if (!documents.some(item => item.status === "Ready")) {
        retrievalStatus.textContent = "No Ready documents are available. Upload documents and wait for processing to finish.";
        return;
    }
    if (!question) {
        retrievalStatus.textContent = "Enter a question before submitting.";
        return;
    }
    if (uploadInProgress || deletingDocumentId) {
        retrievalStatus.textContent = "Wait for the document operation to finish before asking a question.";
        return;
    }
    const documentId = currentDocumentId;
    const scopeName = documentId ? (documents.find(item => item.document_id === documentId)?.file_name || activeDocument.textContent) : "All Ready documents";
    const version = ++retrievalVersion;
    displayedAnswer = null;
    askInProgress = true;
    askButton.disabled = true;
    askButton.textContent = "Asking…";
    questionForm.setAttribute("aria-busy", "true");
    questionInput.disabled = true;
    retrievalResults.replaceChildren();
    answerPanel.hidden = true;
    answerText.textContent = "";
    retrievalStatus.textContent = "Finding relevant passages and generating an answer…";
    try {
        const response = await fetch(`${API_URL}/ask`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(documentId ? {question, document_id: documentId} : {question})
        });
        const data = await response.json().catch(() => null);
        if (!response.ok) {
            throw new Error(typeof data?.detail === "string" ? data.detail : `Could not get an answer (HTTP ${response.status}). Please try again.`);
        }
        if (!data || !Array.isArray(data.results) || data.document_id !== documentId
                || typeof data.answer !== "string" || !data.answer.trim()) {
            throw new Error("The backend returned an invalid or empty answer. Please try again.");
        }
        const entry = {...data, question, file_name: scopeName};
        questionHistory.push(entry);
        renderHistory();
        const current = version === retrievalVersion;
        if (current) {
            renderAnswer(data);
            displayedAnswer = entry;
            retrievalStatus.textContent = `Answer received. Retrieved ${data.results.length} chunks.`;
        }
        addNotification({kind: "llm", question, operation: "answer", result: "success", code: "answer_ready", reason: "Answer generated successfully."});
    } catch (error) {
        const failureReason = error instanceof TypeError
            ? "Could not connect to the backend. Check that it is running and try again."
            : (error.message || "Could not get an answer. Please try again.");
        addNotification({kind: "llm", question, operation: "answer", result: "failure", code: "answer_failed",
            reason: `Answer generation failed. ${failureReason}`});
        if (version === retrievalVersion) retrievalStatus.textContent = failureReason;
    } finally {
        if (version === retrievalVersion) {
            askInProgress = false;
            askButton.textContent = "Ask";
            questionForm.setAttribute("aria-busy", "false");
            askButton.disabled = false;
            questionInput.disabled = false;
        }
    }
});

// Load the existing metadata list on every page visit.
void loadDocuments();
