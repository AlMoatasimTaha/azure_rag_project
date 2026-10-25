const fileInput = document.getElementById("fileInput");
const uploadButton = document.getElementById("uploadButton");
const selectedFile = document.getElementById("selectedFile");
const result = document.getElementById("result");
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
const processingStatuses = new Set(["Valid", "Extracting", "Chunking", "Embedding", "Indexing"]);
let documents = [];
let listVersion = 0;
let uploadInProgress = false;
let deletingDocumentId = null;
const questionForm = document.getElementById("questionForm");
const questionInput = document.getElementById("questionInput");
const askButton = document.getElementById("askButton");
const retrievalStatus = document.getElementById("retrievalStatus");
const retrievalResults = document.getElementById("retrievalResults");
let currentDocumentId = null;
let retrievalVersion = 0;
let activeStatus = "Upload";

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
    currentDocumentId = null;
    retrievalVersion += 1;
    questionPanel.hidden = true;
    activeDocument.textContent = "";
    questionInput.value = "";
    questionInput.disabled = false;
    askButton.disabled = false;
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
    resetRetrieval();
    currentDocumentId = item.document_id;
    activeDocument.textContent = item.file_name || item.document_id;
    questionPanel.hidden = item.status !== "Ready";
    renderDocuments();
}

function renderDocuments() {
    documentList.replaceChildren();
    for (const item of documents) {
        const row = document.createElement("li");
        row.className = "document-item" + (item.document_id === currentDocumentId ? " selected" : "");
        const select = document.createElement("button");
        select.type = "button";
        select.className = "document-select";
        select.disabled = uploadInProgress || !!deletingDocumentId;
        select.setAttribute("aria-pressed", String(item.document_id === currentDocumentId));
        const name = document.createElement("span");
        name.textContent = item.file_name || "Untitled document";
        const status = document.createElement("span");
        status.className = "document-status";
        status.textContent = item.status || "Unknown status";
        select.append(name, status);
        select.addEventListener("click", () => selectDocument(item));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "delete-button";
        remove.textContent = deletingDocumentId === item.document_id ? "Deleting…" : "Delete";
        remove.setAttribute("aria-label", `Delete ${item.file_name || "document"}`);
        remove.disabled = uploadInProgress || !!deletingDocumentId || processingStatuses.has(item.status);
        remove.addEventListener("click", () => deleteDocument(item));
        row.append(select, remove);
        documentList.append(row);
    }
}

async function loadDocuments() {
    const version = ++listVersion;
    refreshDocuments.disabled = true;
    documentsStatus.textContent = "Loading documents…";
    try {
        const response = await fetch(`${API_URL}/documents`);
        const data = await response.json();
        if (version !== listVersion) return;
        if (!response.ok || !Array.isArray(data)) throw new Error(typeof data.detail === "string" ? data.detail : "Could not load documents.");
        documents = data;
        renderDocuments();
        documentsStatus.textContent = documents.length ? `${documents.length} documents` : "No documents yet. Upload one to get started.";
    } catch (error) {
        if (version === listVersion) documentsStatus.textContent = error.message || "Could not connect to the backend.";
    } finally {
        if (version === listVersion) refreshDocuments.disabled = false;
    }
}

async function deleteDocument(item) {
    if (uploadInProgress || deletingDocumentId || !window.confirm(`Delete ${item.file_name || "this document"} and its related data?`)) return;
    deletingDocumentId = item.document_id;
    uploadButton.disabled = true;
    askButton.disabled = true;
    renderDocuments();
    try {
        const response = await fetch(`${API_URL}/documents/${encodeURIComponent(item.document_id)}`, { method: "DELETE" });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Document deletion failed. Please retry.");
        if (currentDocumentId === item.document_id) resetRetrieval();
        await loadDocuments();
        const shared = Object.values(data.blob_cleanup || {}).includes("shared_preserved");
        documentsStatus.textContent = shared ? "Document deleted. Shared files were kept for other documents." : (data.message || "Document deleted.");
    } catch (error) {
        documentsStatus.textContent = error.message || "Document deletion failed. Please retry.";
    } finally {
        deletingDocumentId = null;
        uploadButton.disabled = false;
        askButton.disabled = false;
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
    const file = fileInput.files[0];
    selectedFile.textContent = file ? `Selected: ${file.name}` : "";
    progressPanel.hidden = true;
    result.textContent = "";
    renderDocuments();
});

uploadButton.addEventListener("click", async () => {
    const file = fileInput.files[0];
    if (!file) {
        result.textContent = "Please choose a file.";
        return;
    }
    const formData = new FormData();
    resetRetrieval();
    formData.append("file", file);
    uploadButton.disabled = true;
    fileInput.disabled = true;
    uploadInProgress = true;
    renderDocuments();
    showProgress("Upload", "Uploading and validating document...");
    let completed = false;

    try {
        const response = await fetch(`${API_URL}/upload?progress=true`, {
            method: "POST",
            body: formData
        });
        if (!response.ok) {
            throw new Error(`Backend request failed (HTTP ${response.status}).`);
        }
        if (!(response.headers.get("content-type") || "").includes("application/x-ndjson")) {
            completed = showUpdate(await response.json());
        } else {
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";
            while (true) {
                const { value, done } = await reader.read();
                buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop();
                for (const line of lines) {
                    if (line.trim()) completed = showUpdate(JSON.parse(line)) || completed;
                }
                if (done) break;
            }
            if (buffer.trim()) completed = showUpdate(JSON.parse(buffer)) || completed;
        }
        if (!completed) throw new Error("Processing stopped before a final result. Check the backend status before retrying.");
    } catch (error) {
        if (!completed) {
            showUploadError(error.message || "Could not connect to the backend.");
        }
        console.error(error);
    } finally {
        uploadInProgress = false;
        uploadButton.disabled = false;
        fileInput.disabled = false;
        await loadDocuments();
    }
});

questionForm.addEventListener("submit", async event => {
    event.preventDefault();
    const question = questionInput.value.trim();
    if (!currentDocumentId || !question) {
        retrievalStatus.textContent = "Upload a document successfully and enter a question.";
        return;
    }
    const documentId = currentDocumentId;
    const version = ++retrievalVersion;
    askButton.disabled = true;
    questionInput.disabled = true;
    retrievalResults.replaceChildren();
    retrievalStatus.textContent = "Retrieving relevant chunks...";
    try {
        const response = await fetch(`${API_URL}/ask`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question, document_id: documentId })
        });
        const data = await response.json();
        if (version !== retrievalVersion) return;
        if (!response.ok) {
            throw new Error(typeof data.detail === "string" ? data.detail : `Retrieval failed (HTTP ${response.status}).`);
        }
        if (!Array.isArray(data.results) || data.document_id !== documentId) {
            throw new Error("Invalid retrieval response.");
        }
        data.results.forEach((chunk, index) => {
            const item = document.createElement("li");
            const metadata = document.createElement("strong");
            const score = typeof chunk.score === "number" ? chunk.score.toFixed(4) : "—";
            const pages = Array.isArray(chunk.page_numbers) && chunk.page_numbers.length ? chunk.page_numbers.join(", ") : "—";
            metadata.textContent = `Rank ${index + 1} | Score: ${score} | Page: ${pages} | Section: ${chunk.section_heading || "—"}`;
            const preview = document.createElement("p");
            const content = typeof chunk.content === "string" ? chunk.content.trim() : "";
            preview.textContent = content.slice(0, 500) + (content.length > 500 ? "…" : "");
            item.append(metadata, preview);
            retrievalResults.append(item);
        });
        retrievalStatus.textContent = data.results.length ? `Retrieved ${data.results.length} chunks.` : "No chunks found for this document.";
    } catch (error) {
        if (version === retrievalVersion) retrievalStatus.textContent = error.message || "Could not retrieve chunks.";
    } finally {
        if (version === retrievalVersion) {
            askButton.disabled = false;
            questionInput.disabled = false;
        }
    }
});

// Load the existing metadata list on every page visit.
void loadDocuments();
