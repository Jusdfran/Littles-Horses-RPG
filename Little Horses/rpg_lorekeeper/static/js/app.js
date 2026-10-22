// Estado Global da Aplicação
const state = {
    chatHistory: [],
    diceLog: [],
    characters: [],
    activeCharacterId: null,
    bookChapters: [
        {
            id: 1,
            title: "Introdução e Regras Fundamentais",
            content: "",
            aiPrompt: "",
        },
    ],
};

document.addEventListener("DOMContentLoaded", () => {
    loadSystemStatus();
    loadCharacters();
    recalcSheetDerived();
    renderBookChapters();

    const apiKeyInput = document.getElementById("gemini-api-key");
    if (apiKeyInput) {
        apiKeyInput.addEventListener("input", () => {
            updateApiDot(Boolean(apiKeyInput.value.trim()));
        });
    }
});

// Alternância de Abas
function switchTab(tabId) {
    document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === tabId);
    });
    document.querySelectorAll(".nav-tab").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.tab === tabId);
    });
}

function updateApiDot(isOnline) {
    const dot = document.getElementById("api-status-dot");
    if (!dot) return;
    dot.classList.toggle("online", isOnline);
    dot.classList.toggle("offline", !isOnline);
}

// ==========================================
// STATUS DO SISTEMA E GESTÃO DE LIVROS / PDFs
// ==========================================

async function loadSystemStatus() {
    try {
        const res = await fetch("/api/status");
        const data = await res.json();
        document.getElementById("chunk-count-badge").textContent = data.total_chunks ?? 0;

        const manualKey = document.getElementById("gemini-api-key")?.value.trim();
        updateApiDot(Boolean(data.has_env_api_key || manualKey));

        renderLibraryGrid(data.files_by_category || {});
    } catch (err) {
        console.error("Erro ao carregar status:", err);
    }
}

function renderLibraryGrid(filesByCategory) {
    const grid = document.getElementById("library-categories-grid");
    if (!grid) return;

    const labels = {
        books: "📖 Manuais & Livros em PDF (data/books/)",
        rules: "⚖️ Regras & Tabelas (data/rules/)",
        lore: "🏰 Lore, Mundos & Aventuras (data/lore/)",
        npcs: "🧙 Bestiário, NPCs & Fichas (data/npcs/)",
    };

    grid.innerHTML = Object.entries(labels)
        .map(([cat, title]) => {
            const files = filesByCategory[cat] || [];
            const listHtml = files.length
                ? `<ul class="file-pill-list">${files
                      .map((f) => {
                          const fileName = typeof f === "string" ? f : f.name;
                          const sizeStr = f.size_kb !== undefined ? ` (${f.size_kb} KB)` : "";
                          return `
                              <li class="file-pill-item">
                                  <span class="file-pill-name">📄 ${escapeHtml(fileName)}<span class="muted">${sizeStr}</span></span>
                                  <div class="file-pill-actions">
                                      <a class="btn btn-secondary btn-xs" href="/api/books/download/${encodeURIComponent(cat)}/${encodeURIComponent(fileName)}" title="Baixar arquivo">⬇️</a>
                                      <button type="button" class="icon-btn" onclick="deleteUploadedBook('${escapeHtml(cat)}', '${escapeHtml(fileName)}')" title="Remover arquivo e desindexar">🗑️</button>
                                  </div>
                              </li>
                          `;
                      })
                      .join("")}</ul>`
                : `<p class="muted">Nenhum livro ou documento enviado nesta categoria.</p>`;
            return `
                <div class="card library-cat-card">
                    <h3>${title}</h3>
                    ${listHtml}
                </div>
            `;
        })
        .join("");
}

async function handleBookUpload(event) {
    event.preventDefault();
    const fileInput = document.getElementById("upload-files-input");
    const category = document.getElementById("upload-category")?.value || "books";
    const submitBtn = document.getElementById("upload-submit-btn");
    const feedback = document.getElementById("upload-feedback");

    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        alert("Selecione pelo menos um arquivo (.pdf, .md ou .txt) para enviar.");
        return;
    }

    const formData = new FormData();
    formData.append("category", category);
    for (const file of fileInput.files) {
        formData.append("files", file);
    }

    submitBtn.disabled = true;
    submitBtn.textContent = "⏳ Extraindo páginas e indexando...";
    if (feedback) feedback.textContent = "⏳ Processando PDFs e indexando no ChromaDB...";

    try {
        const res = await fetch("/api/books/upload", {
            method: "POST",
            body: formData,
        });
        const data = await res.json();
        if (data.error) {
            if (feedback) feedback.textContent = `❌ ${data.error}`;
        } else {
            fileInput.value = "";
            if (feedback) {
                feedback.textContent = `✅ ${data.saved_files.length} livro(s) enviado(s) e indexado(s)! (${data.total_chunks} fragmentos ativos no ChromaDB)`;
            }
            await loadSystemStatus();
        }
    } catch (err) {
        if (feedback) feedback.textContent = `❌ Falha no envio: ${err.message}`;
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "📤 Enviar & Indexar Páginas";
    }
}

async function deleteUploadedBook(category, filename) {
    if (!confirm(`Deseja remover '${filename}' de data/${category}/ e atualizar o banco vetorial?`)) {
        return;
    }
    try {
        await fetch(`/api/books/${encodeURIComponent(category)}/${encodeURIComponent(filename)}`, {
            method: "DELETE",
        });
        await loadSystemStatus();
    } catch (err) {
        console.error("Erro ao remover livro:", err);
    }
}

async function triggerReindex() {
    const badge = document.getElementById("chunk-count-badge");
    if (badge) badge.textContent = "⏳...";
    try {
        const res = await fetch("/api/reindex", { method: "POST" });
        const data = await res.json();
        if (badge) badge.textContent = data.total_chunks ?? 0;
        await loadSystemStatus();
    } catch (err) {
        console.error("Erro ao reindexar:", err);
    }
}

// ==========================================
// ESTÚDIO DE CRIAÇÃO DE LIVROS
// ==========================================

function syncChaptersFromDOM() {
    state.bookChapters.forEach((ch, idx) => {
        const titleEl = document.getElementById(`ch-title-${idx}`);
        const contentEl = document.getElementById(`ch-content-${idx}`);
        const promptEl = document.getElementById(`ch-prompt-${idx}`);
        if (titleEl) ch.title = titleEl.value;
        if (contentEl) ch.content = contentEl.value;
        if (promptEl) ch.aiPrompt = promptEl.value;
    });
}

function renderBookChapters() {
    const container = document.getElementById("book-chapters-container");
    if (!container) return;

    container.innerHTML = state.bookChapters
        .map(
            (ch, idx) => `
            <div class="chapter-card">
                <div class="chapter-header-row">
                    <strong>📖 Capítulo ${idx + 1}</strong>
                    <input
                        type="text"
                        id="ch-title-${idx}"
                        value="${escapeHtml(ch.title)}"
                        placeholder="Título do Capítulo (ex: Sistema de Magia, Combate, Cidades...)"
                    />
                    ${
                        state.bookChapters.length > 1
                            ? `<button type="button" class="btn btn-secondary btn-xs" onclick="removeBookChapter(${idx})">🗑️ Remover Capítulo</button>`
                            : ""
                    }
                </div>
                <div class="chapter-ai-bar">
                    <input
                        type="text"
                        id="ch-prompt-${idx}"
                        value="${escapeHtml(ch.aiPrompt || "")}"
                        placeholder="💡 Instrução opcional para o Co-Autor IA (baseado nos PDFs enviados)..."
                    />
                    <button type="button" id="ch-ai-btn-${idx}" class="btn btn-secondary btn-sm" onclick="assistChapterWithAI(${idx})">
                        ✨ Redigir com IA (RAG)
                    </button>
                </div>
                <textarea
                    id="ch-content-${idx}"
                    rows="7"
                    placeholder="Escreva o conteúdo deste capítulo aqui. Ao compilar para PDF, cada capítulo/seção será paginado fisicamente para citação exata pelo Oráculo..."
                >${escapeHtml(ch.content)}</textarea>
            </div>
        `
        )
        .join("");
}

function addBookChapter() {
    syncChaptersFromDOM();
    state.bookChapters.push({
        id: Date.now(),
        title: `Capítulo ${state.bookChapters.length + 1}`,
        content: "",
        aiPrompt: "",
    });
    renderBookChapters();
}

function removeBookChapter(idx) {
    syncChaptersFromDOM();
    if (state.bookChapters.length <= 1) return;
    state.bookChapters.splice(idx, 1);
    renderBookChapters();
}

async function assistChapterWithAI(idx) {
    syncChaptersFromDOM();
    const ch = state.bookChapters[idx];
    const bookTitle = document.getElementById("book-title")?.value.trim() || "Novo Livro de RPG";
    const apiKey = document.getElementById("gemini-api-key")?.value.trim() || "";
    const btn = document.getElementById(`ch-ai-btn-${idx}`);

    if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ Gerando...";
    }

    try {
        const res = await fetch("/api/books/ai-assist", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                book_title: bookTitle,
                chapter_title: ch.title,
                instructions: ch.aiPrompt,
                current_draft: ch.content,
                api_key: apiKey,
            }),
        });
        const data = await res.json();
        if (data.error) {
            alert(data.error);
        } else {
            state.bookChapters[idx].content = data.generated_text || "";
            renderBookChapters();
        }
    } catch (err) {
        alert(`Erro ao consultar IA: ${err.message}`);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = "✨ Redigir com IA (RAG)";
        }
    }
}

async function compileAndSaveBook() {
    syncChaptersFromDOM();
    const title = document.getElementById("book-title")?.value.trim() || "";
    const author = document.getElementById("book-author")?.value.trim() || "Autor";
    const category = document.getElementById("book-category")?.value || "books";
    const format = document.getElementById("book-format")?.value || "pdf";
    const feedback = document.getElementById("book-author-feedback");

    if (!title) {
        alert("Informe o título do livro antes de compilar!");
        return;
    }

    if (feedback) feedback.textContent = "⏳ Compilando páginas do livro e indexando no ChromaDB...";

    try {
        const res = await fetch("/api/books/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                title,
                author,
                category,
                format,
                chapters: state.bookChapters,
            }),
        });
        const data = await res.json();
        if (data.error) {
            if (feedback) feedback.textContent = `❌ ${data.error}`;
            return;
        }
        await loadSystemStatus();
        if (feedback) {
            const pagesInfo = data.pages_generated ? ` (${data.pages_generated} página(s) PDF geradas)` : "";
            feedback.textContent = `✅ Livro '${data.title}' compilado em [${data.created_files.join(", ")}]${pagesInfo} e indexado no RAG!`;
        }
    } catch (err) {
        if (feedback) feedback.textContent = `❌ Erro ao compilar livro: ${err.message}`;
    }
}

// ==========================================
// 1. ORÁCULO RAG (CHAT)
// ==========================================

function sendQuickPrompt(promptText) {
    switchTab("tab-oracle");
    const input = document.getElementById("chat-input");
    input.value = promptText;
    document.getElementById("chat-form").requestSubmit();
}

function clearChatHistory() {
    state.chatHistory = [];
    const container = document.getElementById("chat-messages");
    container.innerHTML = `
        <div class="chat-msg assistant">
            <div class="msg-avatar">🐉</div>
            <div class="msg-bubble">
                <div class="msg-role">Arquivista Oficial</div>
                <div class="msg-text">Histórico limpo! Faça uma pergunta sobre os livros em PDF enviados ou criados no sistema.</div>
            </div>
        </div>
    `;
}

async function handleChatSubmit(event) {
    event.preventDefault();
    const input = document.getElementById("chat-input");
    const submitBtn = document.getElementById("chat-submit-btn");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";
    appendChatMessage("user", message);

    const apiKey = document.getElementById("gemini-api-key")?.value.trim() || "";
    const category = document.getElementById("rag-category")?.value || "Todas";
    const topK = parseInt(document.getElementById("rag-topk")?.value || "5", 10);

    submitBtn.disabled = true;
    submitBtn.textContent = "⏳ Consultando...";

    const loadingId = appendLoadingMessage();

    try {
        const res = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                message,
                api_key: apiKey,
                category,
                top_k: topK,
                history: state.chatHistory,
            }),
        });
        const data = await res.json();
        removeElementById(loadingId);

        if (data.error) {
            appendChatMessage("assistant", `❌ ${data.error}`);
        } else {
            appendChatMessage("assistant", data.reply, data.sources || []);
            state.chatHistory.push({ role: "user", content: message });
            state.chatHistory.push({ role: "assistant", content: data.reply });
        }
    } catch (err) {
        removeElementById(loadingId);
        appendChatMessage("assistant", `❌ Erro de comunicação com o servidor: ${err.message}`);
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "✨ Consultar Oráculo";
    }
}

function appendLoadingMessage() {
    const id = "msg-loading-" + Date.now();
    const container = document.getElementById("chat-messages");
    const div = document.createElement("div");
    div.id = id;
    div.className = "chat-msg assistant";
    div.innerHTML = `
        <div class="msg-avatar">🐉</div>
        <div class="msg-bubble">
            <div class="msg-role">Arquivista Oficial</div>
            <div class="msg-text muted">Consultando páginas dos PDFs indexados no ChromaDB...</div>
        </div>
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return id;
}

function removeElementById(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

function appendChatMessage(role, text, sources = []) {
    const container = document.getElementById("chat-messages");
    const div = document.createElement("div");
    div.className = `chat-msg ${role}`;

    const avatar = role === "user" ? "🛡️" : "🐉";
    const roleLabel = role === "user" ? "Aventureiro" : "Arquivista Oficial";

    let sourcesHtml = "";
    if (role === "assistant" && sources && sources.length > 0) {
        const items = sources
            .map((src) => {
                const citation =
                    src.formatted_citation ||
                    `📖 [${src.source}] - Página ${src.page}: "${src.text}"`;
                return `<div class="source-item">${escapeHtml(citation)}</div>`;
            })
            .join("");

        sourcesHtml = `
            <details class="sources-details" open>
                <summary>📖 Fontes Consultadas (${sources.length})</summary>
                <div class="sources-list">${items}</div>
            </details>
        `;
    }

    div.innerHTML = `
        <div class="msg-avatar">${avatar}</div>
        <div class="msg-bubble">
            <div class="msg-role">${roleLabel}</div>
            <div class="msg-text">${formatSimpleMarkdown(text)}</div>
            ${sourcesHtml}
        </div>
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

// ==========================================
// 2. SISTEMA DE ROLAGEM DE DADOS
// ==========================================

function quickRollDie(sides) {
    const qty = Math.max(1, parseInt(document.getElementById("dice-qty")?.value || "1", 10));
    const mod = parseInt(document.getElementById("dice-mod")?.value || "0", 10);
    const mode = document.getElementById("dice-mode")?.value || "normal";
    const exhaustion = parseInt(document.getElementById("dice-exhaustion")?.value || "0", 10);

    let formula = `${qty}d${sides}`;
    if (mod > 0) formula += `+${mod}`;
    else if (mod < 0) formula += `${mod}`;

    executeRoll({
        formula,
        mode,
        exhaustion,
        label: `Rolagem ${formula.toUpperCase()}`,
    });
}

function rollCustomFormula() {
    const formula = document.getElementById("custom-dice-formula")?.value.trim() || "1d20";
    const label = document.getElementById("custom-dice-label")?.value.trim() || "Fórmula Livre";
    const mode = document.getElementById("dice-mode")?.value || "normal";
    const exhaustion = parseInt(document.getElementById("dice-exhaustion")?.value || "0", 10);

    executeRoll({ formula, mode, exhaustion, label });
}

function rollPreset(formula, label) {
    const exhaustion = parseInt(document.getElementById("char-exhaustion")?.value || "0", 10);
    executeRoll({ formula, mode: "normal", exhaustion, label });
}

async function executeRoll(payload) {
    try {
        const res = await fetch("/api/roll", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (data.error) {
            alert(data.error);
            return;
        }
        updateDiceDisplay(data);
        prependRollLog(data);
    } catch (err) {
        console.error("Erro ao rolar dados:", err);
    }
}

function updateDiceDisplay(roll) {
    const totalEl = document.getElementById("dice-big-total");
    const labelEl = document.getElementById("dice-big-label");
    const breakdownEl = document.getElementById("dice-big-breakdown");
    const alertEl = document.getElementById("dice-house-rule-alert");

    if (!totalEl) return;

    totalEl.classList.remove("rolling-anim", "crit", "fumble");
    void totalEl.offsetWidth;
    totalEl.classList.add("rolling-anim");

    if (roll.is_crit) totalEl.classList.add("crit");
    if (roll.is_fumble) totalEl.classList.add("fumble");

    totalEl.textContent = roll.total;
    labelEl.textContent = `${roll.label} (${roll.formula})`;

    let parts = [`Dados: [${roll.rolls.join(", ")}]`];
    if (roll.dropped_rolls && roll.dropped_rolls.length > 0) {
        parts.push(`Descartados: [${roll.dropped_rolls.join(", ")}]`);
    }
    if (roll.modifier !== 0) {
        parts.push(`Mod: ${roll.modifier > 0 ? "+" + roll.modifier : roll.modifier}`);
    }
    if (roll.exhaustion_penalty !== 0) {
        parts.push(`Penalidade: ${roll.exhaustion_penalty}`);
    }
    breakdownEl.textContent = parts.join(" | ");

    if (roll.house_rule_note) {
        alertEl.textContent = roll.house_rule_note;
        alertEl.className = `house-rule-banner ${roll.is_crit ? "crit" : "fumble"}`;
    } else {
        alertEl.className = "house-rule-banner hidden";
    }
}

function prependRollLog(roll) {
    state.diceLog.unshift(roll);
    const listEl = document.getElementById("dice-log-list");
    if (!listEl) return;

    listEl.innerHTML = state.diceLog
        .slice(0, 25)
        .map((r) => {
            const extraClass = r.is_crit ? "crit" : r.is_fumble ? "fumble" : "";
            const badge = r.is_crit ? "🔥 CRÍTICO 20" : r.is_fumble ? "💀 FALHA 1" : r.formula;
            const modStr = r.modifier ? (r.modifier > 0 ? `+${r.modifier}` : r.modifier) : "";
            const exhStr = r.exhaustion_penalty ? ` (${r.exhaustion_penalty})` : "";
            return `
                <div class="roll-log-item ${extraClass}">
                    <div class="roll-log-top">
                        <span>${escapeHtml(r.label)}</span>
                        <span class="roll-log-total">${r.total}</span>
                    </div>
                    <div class="roll-log-meta">
                        ${escapeHtml(badge)} → [${r.rolls.join(", ")}]${modStr}${exhStr} • ${r.timestamp}
                    </div>
                </div>
            `;
        })
        .join("");
}

function clearDiceLog() {
    state.diceLog = [];
    const listEl = document.getElementById("dice-log-list");
    if (listEl) {
        listEl.innerHTML = `<div class="empty-log">Histórico limpo.</div>`;
    }
}

// ==========================================
// 3. CRIADOR E GESTOR DE FICHAS
// ==========================================

function calcMod(score) {
    return Math.floor((parseInt(score || 10, 10) - 10) / 2);
}

function formatMod(mod) {
    return mod >= 0 ? `+${mod}` : `${mod}`;
}

function recalcSheetDerived() {
    const attrs = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"];
    attrs.forEach((attr) => {
        const val = parseInt(document.getElementById(`attr-${attr}`)?.value || "10", 10);
        const mod = calcMod(val);
        const modEl = document.getElementById(`mod-${attr}`);
        if (modEl) modEl.textContent = formatMod(mod);
    });

    const level = Math.max(1, parseInt(document.getElementById("char-level")?.value || "1", 10));
    const prof = Math.ceil(level / 4) + 1;
    const profEl = document.getElementById("char-proficiency");
    if (profEl) profEl.value = `+${prof}`;

    const exhaustion = parseInt(document.getElementById("char-exhaustion")?.value || "0", 10);
    const diceExhaustion = document.getElementById("dice-exhaustion");
    if (diceExhaustion) diceExhaustion.value = exhaustion;

    const charName = document.getElementById("char-name")?.value.trim() || "Mesa Virtual";
    const pill = document.getElementById("active-char-pill");
    if (pill) {
        pill.textContent = exhaustion > 0 ? `${charName} (-${exhaustion})` : charName;
    }
}

function rollAttributeCheck(attrLabel, attrKey) {
    const score = parseInt(document.getElementById(`attr-${attrKey}`)?.value || "10", 10);
    const mod = calcMod(score);
    const exhaustion = parseInt(document.getElementById("char-exhaustion")?.value || "0", 10);
    const charName = document.getElementById("char-name")?.value.trim() || "Personagem";

    let formula = "1d20";
    if (mod > 0) formula += `+${mod}`;
    else if (mod < 0) formula += `${mod}`;

    executeRoll({
        formula,
        mode: document.getElementById("dice-mode")?.value || "normal",
        exhaustion,
        label: `Teste de ${attrLabel} (${charName})`,
    });
}

async function rollAllAttributes4d6() {
    const attrs = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"];
    for (const attr of attrs) {
        const res = await fetch("/api/roll", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                formula: "4d6kh3",
                label: `Atributo ${attr.toUpperCase()} (4d6kh3)`,
            }),
        });
        const data = await res.json();
        if (!data.error) {
            const input = document.getElementById(`attr-${attr}`);
            if (input) input.value = data.total;
            prependRollLog(data);
        }
    }
    recalcSheetDerived();
}

async function loadCharacters() {
    try {
        const res = await fetch("/api/characters");
        const data = await res.json();
        state.characters = data.characters || [];
        renderSavedCharactersList();
        if (state.characters.length > 0 && !state.activeCharacterId) {
            selectCharacter(state.characters[0].id);
        }
    } catch (err) {
        console.error("Erro ao carregar personagens:", err);
    }
}

function renderSavedCharactersList() {
    const listEl = document.getElementById("saved-characters-list");
    if (!listEl) return;

    if (!state.characters.length) {
        listEl.innerHTML = `<p class="muted">Nenhuma ficha salva ainda. Preencha os dados ao lado e clique em Salvar!</p>`;
        return;
    }

    listEl.innerHTML = state.characters
        .map((c) => {
            const activeClass = c.id === state.activeCharacterId ? "active" : "";
            return `
                <div class="saved-char-item ${activeClass}" onclick="selectCharacter('${escapeHtml(c.id)}')">
                    <div class="saved-char-info">
                        <strong>${escapeHtml(c.name)}</strong>
                        <span>${escapeHtml(c.race)} • ${escapeHtml(c.char_class)} Nv. ${c.level}</span>
                    </div>
                    <button class="icon-btn" onclick="event.stopPropagation(); deleteCharacter('${escapeHtml(c.id)}')" title="Excluir ficha">🗑️</button>
                </div>
            `;
        })
        .join("");
}

function selectCharacter(charId) {
    const char = state.characters.find((c) => c.id === charId);
    if (!char) return;

    state.activeCharacterId = char.id;
    document.getElementById("char-id").value = char.id;
    document.getElementById("char-name").value = char.name || "";
    document.getElementById("char-player").value = char.player || "Jogador";
    document.getElementById("char-race").value = char.race || "";
    document.getElementById("char-class").value = char.char_class || "";
    document.getElementById("char-level").value = char.level || 1;
    document.getElementById("char-background").value = char.background || "";
    document.getElementById("char-alignment").value = char.alignment || "Neutro";
    document.getElementById("char-hp-current").value = char.hp_current ?? 10;
    document.getElementById("char-hp-max").value = char.hp_max ?? 10;
    document.getElementById("char-ac").value = char.ac ?? 10;
    document.getElementById("char-speed").value = char.speed || "9m";
    document.getElementById("char-exhaustion").value = char.exhaustion ?? 0;

    const attrs = char.attributes || {};
    document.getElementById("attr-strength").value = attrs.strength ?? 10;
    document.getElementById("attr-dexterity").value = attrs.dexterity ?? 10;
    document.getElementById("attr-constitution").value = attrs.constitution ?? 10;
    document.getElementById("attr-intelligence").value = attrs.intelligence ?? 10;
    document.getElementById("attr-wisdom").value = attrs.wisdom ?? 10;
    document.getElementById("attr-charisma").value = attrs.charisma ?? 10;

    document.getElementById("char-equipment").value = char.equipment || "";
    document.getElementById("char-spells").value = char.spells || "";
    document.getElementById("char-lore").value = char.lore || "";

    recalcSheetDerived();
    renderSavedCharactersList();
}

function resetCharacterForm() {
    state.activeCharacterId = null;
    document.getElementById("char-id").value = "";
    document.getElementById("char-name").value = "";
    document.getElementById("char-player").value = "Jogador";
    document.getElementById("char-race").value = "Humano";
    document.getElementById("char-class").value = "Aventureiro";
    document.getElementById("char-level").value = 1;
    document.getElementById("char-background").value = "";
    document.getElementById("char-alignment").value = "Neutro";
    document.getElementById("char-hp-current").value = 10;
    document.getElementById("char-hp-max").value = 10;
    document.getElementById("char-ac").value = 10;
    document.getElementById("char-speed").value = "9m";
    document.getElementById("char-exhaustion").value = 0;

    ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"].forEach((a) => {
        document.getElementById(`attr-${a}`).value = 10;
    });

    document.getElementById("char-equipment").value = "";
    document.getElementById("char-spells").value = "";
    document.getElementById("char-lore").value = "";

    recalcSheetDerived();
    renderSavedCharactersList();
}

function collectFormCharacter() {
    return {
        id: document.getElementById("char-id").value.trim() || undefined,
        name: document.getElementById("char-name").value.trim(),
        player: document.getElementById("char-player").value.trim(),
        race: document.getElementById("char-race").value.trim(),
        char_class: document.getElementById("char-class").value.trim(),
        level: parseInt(document.getElementById("char-level").value || "1", 10),
        background: document.getElementById("char-background").value.trim(),
        alignment: document.getElementById("char-alignment").value,
        hp_current: parseInt(document.getElementById("char-hp-current").value || "10", 10),
        hp_max: parseInt(document.getElementById("char-hp-max").value || "10", 10),
        ac: parseInt(document.getElementById("char-ac").value || "10", 10),
        speed: document.getElementById("char-speed").value.trim(),
        exhaustion: parseInt(document.getElementById("char-exhaustion").value || "0", 10),
        attributes: {
            strength: parseInt(document.getElementById("attr-strength").value || "10", 10),
            dexterity: parseInt(document.getElementById("attr-dexterity").value || "10", 10),
            constitution: parseInt(document.getElementById("attr-constitution").value || "10", 10),
            intelligence: parseInt(document.getElementById("attr-intelligence").value || "10", 10),
            wisdom: parseInt(document.getElementById("attr-wisdom").value || "10", 10),
            charisma: parseInt(document.getElementById("attr-charisma").value || "10", 10),
        },
        equipment: document.getElementById("char-equipment").value.trim(),
        spells: document.getElementById("char-spells").value.trim(),
        lore: document.getElementById("char-lore").value.trim(),
        sync_to_rag: document.getElementById("char-sync-rag").checked,
    };
}

async function saveCharacterSheet() {
    const payload = collectFormCharacter();
    if (!payload.name) {
        alert("Informe o nome do personagem antes de salvar!");
        return;
    }

    const feedback = document.getElementById("sheet-save-feedback");
    if (feedback) feedback.textContent = "⏳ Salvando ficha...";

    try {
        const res = await fetch("/api/characters", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (data.error) {
            if (feedback) feedback.textContent = `❌ ${data.error}`;
            return;
        }

        state.characters = data.characters || [];
        state.activeCharacterId = data.character.id;
        document.getElementById("char-id").value = data.character.id;
        document.getElementById("chunk-count-badge").textContent = data.total_chunks ?? 0;

        renderSavedCharactersList();
        await loadSystemStatus();

        if (feedback) {
            const ragMsg = data.markdown_file
                ? `Ficha salva e indexada em data/npcs/${data.markdown_file}!`
                : "Ficha salva com sucesso!";
            feedback.textContent = `✅ ${ragMsg}`;
            setTimeout(() => {
                feedback.textContent = "";
            }, 5000);
        }
    } catch (err) {
        if (feedback) feedback.textContent = `❌ Erro ao salvar: ${err.message}`;
    }
}

async function deleteCharacter(charId) {
    if (!confirm("Deseja remover esta ficha?")) return;
    try {
        const res = await fetch(`/api/characters/${encodeURIComponent(charId)}`, {
            method: "DELETE",
        });
        const data = await res.json();
        state.characters = data.characters || [];
        if (state.activeCharacterId === charId) {
            resetCharacterForm();
        } else {
            renderSavedCharactersList();
        }
        await loadSystemStatus();
    } catch (err) {
        console.error("Erro ao excluir personagem:", err);
    }
}

function exportCharacterJSON() {
    const charData = collectFormCharacter();
    const blob = new Blob([JSON.stringify(charData, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `ficha_${(charData.name || "personagem").toLowerCase().replace(/\s+/g, "_")}.json`;
    a.click();
    URL.revokeObjectURL(url);
}

// ==========================================
// UTILITÁRIOS DE FORMATAÇÃO
// ==========================================

function escapeHtml(str) {
    return String(str ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function formatSimpleMarkdown(text) {
    let html = escapeHtml(text);
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    return html
        .split(/\n\n+/)
        .map((p) => `<p>${p.replace(/\n/g, "<br/>")}</p>`)
        .join("");
}
