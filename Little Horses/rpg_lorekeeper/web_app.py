import json
import math
import os
import random
import re
import textwrap
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory
from google import genai
from google.genai import types
from werkzeug.utils import secure_filename

from ingest import (
    COLLECTION_NAME,
    DATA_DIR,
    DB_DIR,
    EXPECTED_CATEGORIES,
    SUPPORTED_EXTENSIONS,
    get_chroma_collection,
    ingest_all,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv()

CHARACTERS_FILE = DATA_DIR / "characters.json"

SYSTEM_INSTRUCTION = (
    "Você é o Arquivista Oficial e Juiz de Regras de RPG. "
    "Responda baseando-se estritamente no CONTEXTO fornecido dos manuais e notas de lore. "
    "Sempre que citar uma regra ou fato derivado de um PDF, indique explicitamente o livro e a página indicada nos metadados. "
    "Se uma regra não constar no compêndio, alerte o usuário antes de sugerir qualquer interpretação própria."
)

MODEL_NAME = "gemini-2.5-flash"

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # Suporte a PDFs de até 200 MB


def _slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_text.strip().lower()).strip("_")
    return slug or "documento"


def _to_latin1_safe(text: str) -> str:
    """Converte caracteres especiais fora do Latin-1 para equivalentes seguros para PDF Type1."""
    replacements = {
        "—": "-",
        "–": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "•": "*",
        "…": "...",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def build_multipage_pdf(
    title: str,
    author: str,
    chapters: List[Dict[str, str]],
    output_path: Path,
) -> int:
    """
    Gera um arquivo PDF real multi-página (compatível com pypdf.PdfReader) a partir
    dos capítulos criados no Estúdio de Criação de Livros.
    Cada capítulo (ou quebra de página automática) gera uma página numerada física.
    """
    pages_lines: List[List[str]] = []

    for ch_idx, ch in enumerate(chapters, start=1):
        ch_title = (ch.get("title") or f"Capítulo {ch_idx}").strip()
        ch_body = (ch.get("content") or "").strip()
        if not ch_body and not ch_title:
            continue

        wrapped_lines: List[str] = [
            f"{title.upper()} - {author}",
            f"CAPITULO {ch_idx}: {ch_title}",
            "-" * 72,
            "",
        ]
        for paragraph in ch_body.splitlines():
            clean_p = paragraph.strip()
            if not clean_p:
                wrapped_lines.append("")
            else:
                wrapped_lines.extend(textwrap.wrap(clean_p, width=82))

        # Divide em páginas físicas de até 42 linhas por página
        lines_per_page = 42
        for start in range(0, max(1, len(wrapped_lines)), lines_per_page):
            chunk_lines = wrapped_lines[start : start + lines_per_page]
            pages_lines.append(chunk_lines)

    if not pages_lines:
        pages_lines = [[title, f"Autor: {author}", "Livro criado no RPG Lorekeeper."]]

    objects: List[str] = []

    def add_obj(content: str) -> int:
        objects.append(content)
        return len(objects)

    add_obj("<< /Type /Catalog /Pages 2 0 R >>")
    add_obj("")  # Placeholder para /Pages
    font_obj_id = add_obj(
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )

    page_obj_ids: List[int] = []
    for page_num, lines in enumerate(pages_lines, start=1):
        stream_cmds = ["BT", "/F1 11 Tf", "50 750 Td", "15 TL"]
        for line in lines:
            safe_line = (
                _to_latin1_safe(line)
                .replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
            )
            stream_cmds.append(f"({safe_line}) Tj T*")
        footer = f"--- Pagina {page_num} ---"
        stream_cmds.append(f"({footer}) Tj")
        stream_cmds.append("ET")
        stream_data = "\n".join(stream_cmds)
        stream_bytes = stream_data.encode("latin-1", errors="replace")

        content_id = add_obj(
            f"<< /Length {len(stream_bytes)} >>\nstream\n{stream_data}\nendstream"
        )
        page_id = add_obj(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_obj_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        )
        page_obj_ids.append(page_id)

    kids_str = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_obj_ids)} >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj_body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{idx} 0 obj\n{obj_body}\nendobj\n".encode("latin-1", errors="replace"))

    xref_offset = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))

    out.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("latin-1")
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(bytes(out))
    return len(page_obj_ids)


def _calc_mod(score: int) -> int:
    return math.floor((int(score) - 10) / 2)


def _format_mod(mod: int) -> str:
    return f"+{mod}" if mod >= 0 else str(mod)


def _load_characters_list() -> List[Dict[str, Any]]:
    """Carrega a lista de fichas salvas pelo usuário (inicia vazia, sem dados fictícios)."""
    if CHARACTERS_FILE.exists():
        try:
            return json.loads(CHARACTERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_characters_list(chars: List[Dict[str, Any]]) -> None:
    CHARACTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHARACTERS_FILE.write_text(
        json.dumps(chars, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _export_character_to_markdown(char: Dict[str, Any]) -> Path:
    """Gera um arquivo .md em data/npcs/ para que a ficha seja indexada no RAG."""
    attrs = char.get("attributes", {})
    str_v = int(attrs.get("strength", 10))
    dex_v = int(attrs.get("dexterity", 10))
    con_v = int(attrs.get("constitution", 10))
    int_v = int(attrs.get("intelligence", 10))
    wis_v = int(attrs.get("wisdom", 10))
    cha_v = int(attrs.get("charisma", 10))

    md_content = (
        f"# Ficha de Personagem: {char.get('name', 'Sem Nome')}\n\n"
        f"## Informações Básicas\n"
        f"- **Jogador:** {char.get('player', 'Desconhecido')}\n"
        f"- **Raça / Origem:** {char.get('race', 'Não informada')}\n"
        f"- **Classe / Arquétipo:** {char.get('char_class', 'Aventureiro')} (Nível {char.get('level', 1)})\n"
        f"- **Antecedente:** {char.get('background', 'Nenhum')}\n"
        f"- **Alinhamento:** {char.get('alignment', 'Neutro')}\n"
        f"- **Pontos de Vida (PV):** {char.get('hp_current', 10)} / {char.get('hp_max', 10)}\n"
        f"- **Classe de Armadura (CA):** {char.get('ac', 10)}\n"
        f"- **Deslocamento:** {char.get('speed', '9m')}\n"
        f"- **Exaustão:** {char.get('exhaustion', 0)}\n\n"
        f"## Atributos e Modificadores\n"
        f"- **Força (FOR):** {str_v} ({_format_mod(_calc_mod(str_v))})\n"
        f"- **Destreza (DES):** {dex_v} ({_format_mod(_calc_mod(dex_v))})\n"
        f"- **Constituição (CON):** {con_v} ({_format_mod(_calc_mod(con_v))})\n"
        f"- **Inteligência (INT):** {int_v} ({_format_mod(_calc_mod(int_v))})\n"
        f"- **Sabedoria (SAB):** {wis_v} ({_format_mod(_calc_mod(wis_v))})\n"
        f"- **Carisma (CAR):** {cha_v} ({_format_mod(_calc_mod(cha_v))})\n\n"
        f"## Equipamento e Inventário\n"
        f"{char.get('equipment', 'Nenhum equipamento registrado.')}\n\n"
        f"## Magias, Habilidades e Talentos\n"
        f"{char.get('spells', 'Nenhuma magia ou habilidade registrada.')}\n\n"
        f"## História e Anotações\n"
        f"{char.get('lore', 'Sem notas adicionais.')}\n"
    )

    npcs_dir = DATA_DIR / "npcs"
    npcs_dir.mkdir(parents=True, exist_ok=True)
    file_path = npcs_dir / f"ficha_{_slugify(char.get('name', 'heroi'))}.md"
    file_path.write_text(md_content, encoding="utf-8")
    return file_path


def retrieve_rag_chunks(
    query: str, n_results: int = 5, category_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    collection = get_chroma_collection()
    total_docs = collection.count()
    if total_docs == 0:
        return []

    effective_n = min(max(int(n_results), 1), min(10, total_docs))
    where_clause = (
        {"category": category_filter}
        if category_filter and category_filter != "Todas"
        else None
    )

    try:
        results = collection.query(
            query_texts=[query],
            n_results=effective_n,
            where=where_clause,
        )
    except Exception:
        results = collection.query(
            query_texts=[query],
            n_results=effective_n,
        )

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    items: List[Dict[str, Any]] = []
    for idx, doc_text in enumerate(documents):
        meta = metadatas[idx] if idx < len(metadatas) and metadatas[idx] else {}
        dist = distances[idx] if idx < len(distances) else None
        source_name = meta.get("source", "Desconhecido")
        page_num = meta.get("page", "N/A")
        raw_snippet = " ".join(doc_text.split())
        snippet = raw_snippet[:280] + "..." if len(raw_snippet) > 280 else raw_snippet
        items.append(
            {
                "text": doc_text,
                "source": source_name,
                "page": page_num,
                "category": meta.get("category", "general"),
                "distance": round(float(dist), 4) if dist is not None else None,
                "formatted_citation": f'📖 [{source_name}] - Página {page_num}: "{snippet}"',
            }
        )
    return items


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    collection = get_chroma_collection()
    files_by_cat: Dict[str, List[Dict[str, Any]]] = {cat: [] for cat in EXPECTED_CATEGORIES}
    for cat in EXPECTED_CATEGORIES:
        cat_dir = DATA_DIR / cat
        if cat_dir.exists():
            for p in sorted(cat_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
                    size_kb = round(p.stat().st_size / 1024, 1)
                    files_by_cat[cat].append(
                        {
                            "name": p.name,
                            "category": cat,
                            "size_kb": size_kb,
                            "ext": p.suffix.lower(),
                        }
                    )

    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    has_env_key = bool(env_key and env_key != "sua_chave_api_aqui")

    return jsonify(
        {
            "collection": COLLECTION_NAME,
            "total_chunks": collection.count(),
            "model": MODEL_NAME,
            "has_env_api_key": has_env_key,
            "files_by_category": files_by_cat,
        }
    )


@app.route("/api/reindex", methods=["POST"])
def api_reindex():
    indexed = ingest_all()
    collection = get_chroma_collection()
    return jsonify(
        {
            "status": "ok",
            "processed_chunks": indexed,
            "total_chunks": collection.count(),
        }
    )


@app.route("/api/books/upload", methods=["POST"])
def api_upload_books():
    """
    Recebe um ou mais arquivos (.pdf, .md, .txt) enviados pelo usuário,
    armazena na subpasta selecionada em data/ e executa ingest_all() imediatamente.
    """
    category = (request.form.get("category") or "books").strip().lower()
    if category not in EXPECTED_CATEGORIES:
        category = "books"

    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        return jsonify({"error": "Nenhum arquivo enviado."}), 400

    target_dir = DATA_DIR / category
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_names: List[str] = []
    for file_storage in uploaded_files:
        if not file_storage or not file_storage.filename:
            continue
        safe_name = secure_filename(file_storage.filename)
        ext = Path(safe_name).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        dest_path = target_dir / safe_name
        file_storage.save(str(dest_path))
        saved_names.append(safe_name)

    if not saved_names:
        return (
            jsonify(
                {
                    "error": "Nenhum arquivo válido (.pdf, .md, .txt) foi encontrado no envio."
                }
            ),
            400,
        )

    ingest_all()
    total_chunks = get_chroma_collection().count()

    return jsonify(
        {
            "status": "ok",
            "saved_files": saved_names,
            "category": category,
            "total_chunks": total_chunks,
        }
    )


@app.route("/api/books/create", methods=["POST"])
def api_create_book():
    """
    Ponto de Criação de Livros: permite criar e compilar um novo livro ou suplemento
    em PDF (com páginas físicas numeradas) e/ou Markdown diretamente em data/,
    indexando-o imediatamente no ChromaDB.
    """
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    author = (data.get("author") or "Autor do Sistema").strip()
    category = (data.get("category") or "books").strip().lower()
    output_format = (data.get("format") or "pdf").strip().lower()  # pdf | md | both
    chapters = data.get("chapters") or []

    if not title:
        return jsonify({"error": "O título do livro é obrigatório."}), 400
    if category not in EXPECTED_CATEGORIES:
        category = "books"

    valid_chapters = [
        {
            "title": (c.get("title") or f"Capítulo {i+1}").strip(),
            "content": (c.get("content") or "").strip(),
        }
        for i, c in enumerate(chapters)
        if (c.get("title") or "").strip() or (c.get("content") or "").strip()
    ]
    if not valid_chapters:
        return (
            jsonify({"error": "Adicione pelo menos um capítulo com conteúdo ao livro."}),
            400,
        )

    slug = _slugify(title)
    created_files: List[str] = []
    pages_generated = 0

    if output_format in ("pdf", "both"):
        pdf_dir = DATA_DIR / ("books" if output_format == "both" else category)
        pdf_path = pdf_dir / f"{slug}.pdf"
        pages_generated = build_multipage_pdf(
            title=title,
            author=author,
            chapters=valid_chapters,
            output_path=pdf_path,
        )
        created_files.append(f"{pdf_dir.name}/{pdf_path.name}")

    if output_format in ("md", "both"):
        md_dir = DATA_DIR / category
        md_dir.mkdir(parents=True, exist_ok=True)
        md_path = md_dir / f"{slug}.md"
        md_blocks = [f"# {title}\n**Autor / Sistema:** {author}\n"]
        for idx, ch in enumerate(valid_chapters, start=1):
            md_blocks.append(f"## Capítulo {idx}: {ch['title']}\n\n{ch['content']}\n")
        md_path.write_text("\n".join(md_blocks), encoding="utf-8")
        created_files.append(f"{md_dir.name}/{md_path.name}")

    ingest_all()
    total_chunks = get_chroma_collection().count()

    return jsonify(
        {
            "status": "ok",
            "title": title,
            "created_files": created_files,
            "pages_generated": pages_generated,
            "total_chunks": total_chunks,
        }
    )


@app.route("/api/books/ai-assist", methods=["POST"])
def api_book_ai_assist():
    """
    Assistente de escrita de capítulos: utiliza os PDFs já enviados ao ChromaDB
    para ajudar o autor a redigir ou expandir um capítulo/regra com o Gemini 2.5 Flash.
    """
    data = request.get_json(silent=True) or {}
    book_title = (data.get("book_title") or "Novo Livro de RPG").strip()
    chapter_title = (data.get("chapter_title") or "Novo Capítulo").strip()
    instructions = (data.get("instructions") or "").strip()
    current_draft = (data.get("current_draft") or "").strip()
    client_api_key = (data.get("api_key") or "").strip()

    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    api_key = client_api_key or (env_key if env_key != "sua_chave_api_aqui" else "")
    if not api_key:
        return (
            jsonify(
                {
                    "error": "Informe sua GEMINI_API_KEY no topo da página ou no arquivo .env para usar o Co-Autor IA."
                }
            ),
            400,
        )

    search_query = f"{book_title} {chapter_title} {instructions}"
    retrieved_chunks = retrieve_rag_chunks(query=search_query, n_results=5)
    context_text = (
        "\n\n".join(
            [
                f"[{c['source']} - Pág. {c['page']}]: {c['text']}"
                for c in retrieved_chunks
            ]
        )
        if retrieved_chunks
        else "Nenhum PDF de referência enviado ainda. Crie conteúdo original coeso."
    )

    prompt = (
        f"Você é um Co-Autor e Designer de Livros de RPG assistindo na criação do livro '{book_title}'.\n"
        f"Baseie-se no contexto abaixo extraído dos PDFs/documentos já enviados ao sistema:\n"
        f"{context_text}\n\n"
        f"Capítulo: {chapter_title}\n"
        f"Instruções do autor: {instructions or 'Desenvolva este capítulo com regras claras e descrições ricas.'}\n"
        f"Rascunho atual:\n{current_draft or '(vazio)'}\n\n"
        f"Escreva o texto completo deste capítulo em português, pronto para ser incluído no livro."
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5),
        )
        return jsonify(
            {
                "status": "ok",
                "generated_text": response.text or "",
                "sources_used": retrieved_chunks,
            }
        )
    except Exception as exc:
        return jsonify({"error": f"Erro na API Gemini: {exc}"}), 500


@app.route("/api/books/<category>/<filename>", methods=["DELETE"])
def api_delete_book(category: str, filename: str):
    if category not in EXPECTED_CATEGORIES:
        return jsonify({"error": "Categoria inválida."}), 400
    safe_name = secure_filename(filename)
    target_path = DATA_DIR / category / safe_name
    if target_path.exists() and target_path.is_file():
        target_path.unlink()
    ingest_all()
    return jsonify(
        {
            "status": "ok",
            "total_chunks": get_chroma_collection().count(),
        }
    )


@app.route("/api/books/download/<category>/<filename>", methods=["GET"])
def api_download_book(category: str, filename: str):
    if category not in EXPECTED_CATEGORIES:
        return jsonify({"error": "Categoria inválida."}), 400
    safe_name = secure_filename(filename)
    return send_from_directory(str(DATA_DIR / category), safe_name, as_attachment=True)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    user_query = (data.get("message") or "").strip()
    client_api_key = (data.get("api_key") or "").strip()
    top_k = int(data.get("top_k", 5))
    category = data.get("category", "Todas")
    history = data.get("history", [])

    if not user_query:
        return jsonify({"error": "A pergunta não pode estar vazia."}), 400

    collection = get_chroma_collection()
    if collection.count() == 0:
        return jsonify(
            {
                "reply": (
                    "📚 **Nenhum livro ou documento indexado ainda.**\n\n"
                    "Envie seus livros em **PDF** (ou `.md`/`.txt`) na aba **📤 Enviar & Gerenciar Livros** "
                    "ou crie um novo livro na aba **✍️ Criação de Livros** para que eu possa responder "
                    "com base nos seus manuais e citar as páginas correspondentes!"
                ),
                "sources": [],
                "used_llm": False,
            }
        )

    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    api_key = client_api_key or (env_key if env_key != "sua_chave_api_aqui" else "")

    retrieved_chunks = retrieve_rag_chunks(
        query=user_query, n_results=top_k, category_filter=category
    )

    if not api_key:
        fallback_reply = (
            "⚠️ **Chave `GEMINI_API_KEY` não configurada.**\n\n"
            "Insira sua chave da API do Google Gemini no painel superior (ou no arquivo `.env`) "
            f"para gerar respostas interpretadas pelo modelo `{MODEL_NAME}`.\n\n"
            "Abaixo estão os trechos e páginas mais relevantes encontrados nos seus livros indexados no **ChromaDB**:"
        )
        return jsonify(
            {
                "reply": fallback_reply,
                "sources": retrieved_chunks,
                "used_llm": False,
            }
        )

    formatted_blocks = []
    for i, item in enumerate(retrieved_chunks, start=1):
        formatted_blocks.append(
            f"--- FRAGMENTO {i} ---\n"
            f"Fonte (source): {item['source']}\n"
            f"Página (page): {item['page']}\n"
            f"Categoria (category): {item['category']}\n"
            f"Conteúdo:\n{item['text']}\n"
        )
    context_block = (
        "\n".join(formatted_blocks)
        if formatted_blocks
        else "NENHUM FRAGMENTO ENCONTRADO NO COMPÊNDIO."
    )

    recent_turns = []
    for msg in history[-4:]:
        role_label = "Usuário" if msg.get("role") == "user" else "Arquivista"
        recent_turns.append(f"{role_label}: {msg.get('content', '')}")
    history_text = (
        "\n".join(recent_turns) if recent_turns else "Sem histórico anterior."
    )

    prompt = (
        f"### CONTEXTO DO COMPÊNDIO (MANUAIS E NOTAS DE LORE)\n"
        f"{context_block}\n\n"
        f"### HISTÓRICO RECENTE DA CONVERSA\n"
        f"{history_text}\n\n"
        f"### PERGUNTA ATUAL DO USUÁRIO\n"
        f"{user_query}\n\n"
        f"Responda em português, seguindo rigorosamente suas instruções de Arquivista Oficial e Juiz de Regras de RPG."
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.2,
            ),
        )
        reply_text = response.text or "Não foi possível gerar uma resposta."
        used_llm = True
    except Exception as exc:
        reply_text = (
            f"❌ **Erro ao consultar a API Gemini (`{MODEL_NAME}`):** `{exc}`\n\n"
            "Verifique se sua `GEMINI_API_KEY` é válida. Os fragmentos recuperados do ChromaDB estão disponíveis abaixo:"
        )
        used_llm = False

    return jsonify(
        {
            "reply": reply_text,
            "sources": retrieved_chunks,
            "used_llm": used_llm,
        }
    )


@app.route("/api/roll", methods=["POST"])
def api_roll():
    data = request.get_json(silent=True) or {}
    formula = (data.get("formula") or "1d20").strip().lower().replace(" ", "")
    mode = (data.get("mode") or "normal").lower()
    label = (data.get("label") or "Rolagem de Dados").strip()
    exhaustion = max(0, min(6, int(data.get("exhaustion", 0))))

    pattern = r"^(\d+)d(\d+)(?:kh(\d+))?([+-]\d+)?$"
    match = re.match(pattern, formula)
    if not match:
        return (
            jsonify(
                {
                    "error": f"Fórmula inválida '{formula}'. Use formatos como 1d20+4, 2d6+3 ou 4d6kh3."
                }
            ),
            400,
        )

    num_dice = min(int(match.group(1)), 100)
    sides = int(match.group(2))
    keep_highest = int(match.group(3)) if match.group(3) else None
    modifier = int(match.group(4)) if match.group(4) else 0

    if num_dice < 1 or sides < 2 or sides > 1000:
        return jsonify({"error": "Quantidade ou faces de dados fora do limite."}), 400

    dropped_rolls: List[int] = []
    if num_dice == 1 and sides == 20 and mode in ("advantage", "disadvantage"):
        r1 = random.randint(1, 20)
        r2 = random.randint(1, 20)
        if mode == "advantage":
            kept, dropped = max(r1, r2), min(r1, r2)
        else:
            kept, dropped = min(r1, r2), max(r1, r2)
        rolls = [kept]
        dropped_rolls = [dropped]
        all_raw_rolls = [r1, r2]
    else:
        all_raw_rolls = [random.randint(1, sides) for _ in range(num_dice)]
        if keep_highest and 0 < keep_highest < num_dice:
            sorted_with_idx = sorted(
                enumerate(all_raw_rolls), key=lambda x: x[1], reverse=True
            )
            kept_indices = {idx for idx, _ in sorted_with_idx[:keep_highest]}
            rolls = [
                val for idx, val in enumerate(all_raw_rolls) if idx in kept_indices
            ]
            dropped_rolls = [
                val for idx, val in enumerate(all_raw_rolls) if idx not in kept_indices
            ]
        else:
            rolls = list(all_raw_rolls)

    dice_sum = sum(rolls)
    exhaustion_penalty = -exhaustion if sides == 20 and exhaustion > 0 else 0
    total = dice_sum + modifier + exhaustion_penalty

    is_crit = sides == 20 and num_dice == 1 and rolls[0] == 20
    is_fumble = sides == 20 and num_dice == 1 and rolls[0] == 1

    house_rule_note = None
    if is_crit:
        house_rule_note = "🔥 20 NATURAL (Sucesso / Acerto Crítico)! Consulte o manual do seu sistema para os efeitos de crítico."
    elif is_fumble:
        house_rule_note = "💀 1 NATURAL (Falha Crítica)! Consulte o manual do seu sistema para os efeitos de falha crítica."

    return jsonify(
        {
            "label": label,
            "formula": formula,
            "mode": mode,
            "sides": sides,
            "num_dice": num_dice,
            "rolls": rolls,
            "all_raw_rolls": all_raw_rolls,
            "dropped_rolls": dropped_rolls,
            "modifier": modifier,
            "exhaustion_penalty": exhaustion_penalty,
            "dice_sum": dice_sum,
            "total": total,
            "is_crit": is_crit,
            "is_fumble": is_fumble,
            "house_rule_note": house_rule_note,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }
    )


@app.route("/api/characters", methods=["GET"])
def api_get_characters():
    chars = _load_characters_list()
    return jsonify({"characters": chars})


@app.route("/api/characters", methods=["POST"])
def api_save_character():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "O nome do personagem é obrigatório."}), 400

    char_id = (data.get("id") or _slugify(name)).strip()
    sync_to_rag = bool(data.get("sync_to_rag", False))

    char_obj: Dict[str, Any] = {
        "id": char_id,
        "name": name,
        "player": (data.get("player") or "Jogador").strip(),
        "race": (data.get("race") or "Humano").strip(),
        "char_class": (data.get("char_class") or "Guerreiro").strip(),
        "level": max(1, min(20, int(data.get("level", 1)))),
        "background": (data.get("background") or "Aventureiro").strip(),
        "alignment": (data.get("alignment") or "Neutro").strip(),
        "hp_current": int(data.get("hp_current", 10)),
        "hp_max": int(data.get("hp_max", 10)),
        "ac": int(data.get("ac", 10)),
        "speed": (data.get("speed") or "9m").strip(),
        "exhaustion": max(0, min(6, int(data.get("exhaustion", 0)))),
        "attributes": {
            "strength": int(data.get("attributes", {}).get("strength", 10)),
            "dexterity": int(data.get("attributes", {}).get("dexterity", 10)),
            "constitution": int(data.get("attributes", {}).get("constitution", 10)),
            "intelligence": int(data.get("attributes", {}).get("intelligence", 10)),
            "wisdom": int(data.get("attributes", {}).get("wisdom", 10)),
            "charisma": int(data.get("attributes", {}).get("charisma", 10)),
        },
        "equipment": (data.get("equipment") or "").strip(),
        "spells": (data.get("spells") or "").strip(),
        "lore": (data.get("lore") or "").strip(),
        "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }

    chars = _load_characters_list()
    updated = False
    for idx, existing in enumerate(chars):
        if existing.get("id") == char_id:
            chars[idx] = char_obj
            updated = True
            break
    if not updated:
        chars.append(char_obj)

    _save_characters_list(chars)

    md_file = None
    if sync_to_rag:
        md_path = _export_character_to_markdown(char_obj)
        md_file = md_path.name
        ingest_all()

    total_chunks = get_chroma_collection().count()

    return jsonify(
        {
            "status": "ok",
            "character": char_obj,
            "characters": chars,
            "markdown_file": md_file,
            "total_chunks": total_chunks,
        }
    )


@app.route("/api/characters/<char_id>", methods=["DELETE"])
def api_delete_character(char_id: str):
    chars = _load_characters_list()
    new_chars = [c for c in chars if c.get("id") != char_id]
    _save_characters_list(new_chars)
    md_path = DATA_DIR / "npcs" / f"ficha_{char_id}.md"
    if md_path.exists():
        md_path.unlink()
        ingest_all()
    return jsonify({"status": "ok", "characters": new_chars})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
