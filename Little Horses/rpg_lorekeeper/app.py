import os
from pathlib import Path
from typing import List, Dict, Any, Optional

import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types

from ingest import (
    ingest_all,
    get_chroma_collection as _init_chroma_collection,
    DB_DIR,
    DATA_DIR,
    COLLECTION_NAME,
)

# Carrega variáveis de ambiente do arquivo .env (se existir)
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv()

# System Instruction obrigatória anti-alucinação
SYSTEM_INSTRUCTION = (
    "Você é o Arquivista Oficial e Juiz de Regras de RPG. "
    "Responda baseando-se estritamente no CONTEXTO fornecido dos manuais e notas de lore. "
    "Sempre que citar uma regra ou fato derivado de um PDF, indique explicitamente o livro e a página indicada nos metadados. "
    "Se uma regra não constar no compêndio, alerte o usuário antes de sugerir qualquer interpretação própria."
)

MODEL_NAME = "gemini-2.5-flash"


@st.cache_resource
def get_chroma_collection():
    """Conecta ao banco vetorial persistente ChromaDB e retorna a coleção."""
    return _init_chroma_collection()


def retrieve_context(
    query: str, n_results: int = 5, category_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Recupera os N fragmentos mais relevantes do ChromaDB (entre 4 e 6 por padrão).
    Retorna lista de dicionários com texto e metadados (source, page, category).
    """
    collection = get_chroma_collection()
    total_docs = collection.count()
    if total_docs == 0:
        return []

    effective_n = min(n_results, total_docs)
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
        # Caso o filtro de categoria não encontre documentos suficientes, busca geral
        results = collection.query(
            query_texts=[query],
            n_results=effective_n,
        )

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    retrieved_items: List[Dict[str, Any]] = []
    for idx, doc_text in enumerate(documents):
        meta = metadatas[idx] if idx < len(metadatas) and metadatas[idx] else {}
        dist = distances[idx] if idx < len(distances) else None
        retrieved_items.append(
            {
                "text": doc_text,
                "source": meta.get("source", "Desconhecido"),
                "page": meta.get("page", "N/A"),
                "category": meta.get("category", "general"),
                "distance": dist,
            }
        )

    return retrieved_items


def format_context_for_prompt(chunks: List[Dict[str, Any]]) -> str:
    """Formata os fragmentos recuperados com seus metadados para envio ao Gemini."""
    if not chunks:
        return "NENHUM FRAGMENTO ENCONTRADO NO COMPÊNDIO."

    formatted_blocks = []
    for i, item in enumerate(chunks, start=1):
        formatted_blocks.append(
            f"--- FRAGMENTO {i} ---\n"
            f"Fonte (source): {item['source']}\n"
            f"Página (page): {item['page']}\n"
            f"Categoria (category): {item['category']}\n"
            f"Conteúdo:\n{item['text']}\n"
        )
    return "\n".join(formatted_blocks)


def render_sources_expander(sources: List[Dict[str, Any]]) -> None:
    """
    Renderiza o expander 'Fontes Consultadas' no formato obrigatório:
    📖 [Nome_do_Livro.pdf] - Página 42: "Trecho do texto..."
    """
    with st.expander("Fontes Consultadas"):
        if not sources:
            st.info("Nenhuma fonte encontrada no banco vetorial para esta consulta.")
            return

        for src in sources:
            source_name = src.get("source", "Desconhecido")
            page_num = src.get("page", "N/A")
            raw_snippet = " ".join(src.get("text", "").split())
            snippet = (
                raw_snippet[:280] + "..." if len(raw_snippet) > 280 else raw_snippet
            )
            st.markdown(f'📖 **[{source_name}] - Página {page_num}:** "{snippet}"')


def generate_lorekeeper_answer(
    api_key: str,
    user_query: str,
    retrieved_chunks: List[Dict[str, Any]],
    chat_history: List[Dict[str, Any]],
) -> str:
    """Invoca o modelo gemini-2.5-flash usando o SDK oficial google-genai."""
    client = genai.Client(api_key=api_key)
    context_block = format_context_for_prompt(retrieved_chunks)

    # Inclui resumo breve das últimas interações para continuidade conversacional
    recent_turns = []
    for msg in chat_history[-4:]:
        role_label = "Usuário" if msg["role"] == "user" else "Arquivista"
        recent_turns.append(f"{role_label}: {msg['content']}")
    history_text = "\n".join(recent_turns) if recent_turns else "Sem histórico anterior."

    prompt = (
        f"### CONTEXTO DO COMPÊNDIO (MANUAIS E NOTAS DE LORE)\n"
        f"{context_block}\n\n"
        f"### HISTÓRICO RECENTE DA CONVERSA\n"
        f"{history_text}\n\n"
        f"### PERGUNTA ATUAL DO USUÁRIO\n"
        f"{user_query}\n\n"
        f"Responda em português, seguindo rigorosamente suas instruções de Arquivista Oficial e Juiz de Regras de RPG."
    )

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.2,
        ),
    )

    return response.text or "Não foi possível gerar uma resposta."


def main() -> None:
    st.set_page_config(
        page_title="RPG Lorekeeper | Compêndio RAG",
        page_icon="🐉",
        layout="wide",
    )

    st.title("🐉 RPG Lorekeeper — Arquivista Oficial & Juiz de Regras")
    st.caption(
        "Compêndio Inteligente com RAG Local (ChromaDB) + Google Gemini (`gemini-2.5-flash`)"
    )

    # Barra lateral de configurações e status do banco
    with st.sidebar:
        st.header("⚙️ Configurações do Grimório")

        env_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if env_api_key and env_api_key != "sua_chave_api_aqui":
            api_key = env_api_key
            st.success("🔑 `GEMINI_API_KEY` carregada do ambiente `.env`.")
        else:
            api_key = st.text_input(
                "🔑 Insira sua GEMINI_API_KEY",
                type="password",
                help="Configure no arquivo .env ou informe aqui para usar o Gemini 2.5 Flash.",
            ).strip()
            if not api_key:
                st.warning("Informe sua `GEMINI_API_KEY` para consultar o oráculo.")

        st.divider()
        st.subheader("📚 Base de Conhecimento")
        collection = get_chroma_collection()
        doc_count = collection.count()
        st.metric("Fragmentos Indexados", doc_count)

        top_k = st.slider(
            "Fragmentos recuperados (Top-K)",
            min_value=4,
            max_value=6,
            value=5,
            help="Quantidade de trechos mais relevantes recuperados do ChromaDB por pergunta.",
        )

        category_filter = st.selectbox(
            "Filtrar por Categoria",
            options=["Todas", "books", "lore", "npcs", "rules"],
            index=0,
        )

        st.divider()
        st.subheader("📤 Enviar Livros (PDF / MD / TXT)")
        upload_category = st.selectbox(
            "Pasta de destino do envio",
            options=["books", "rules", "lore", "npcs"],
            index=0,
        )
        uploaded_files = st.file_uploader(
            "Selecione manuais em PDF ou notas (.md, .txt)",
            type=["pdf", "md", "txt"],
            accept_multiple_files=True,
        )
        if uploaded_files and st.button("📥 Salvar e Indexar Arquivos Enviados", use_container_width=True):
            target_dir = DATA_DIR / upload_category
            target_dir.mkdir(parents=True, exist_ok=True)
            for uf in uploaded_files:
                (target_dir / uf.name).write_bytes(uf.getvalue())
            with st.spinner("Extraindo páginas dos PDFs e indexando no ChromaDB..."):
                st.cache_resource.clear()
                indexed = ingest_all()
                st.success(f"{len(uploaded_files)} arquivo(s) salvo(s)! Total de {indexed} fragmentos ativos.")
                st.rerun()

        if st.button("🔄 Reindexar Arquivos (`data/`)", use_container_width=True):
            with st.spinner("Processando arquivos .md, .txt e .pdf em data/..."):
                st.cache_resource.clear()
                indexed = ingest_all()
                st.success(f"Indexação concluída! ({indexed} fragmentos processados)")
                st.rerun()

        if st.button("🗑️ Limpar Histórico de Chat", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

        st.divider()
        st.markdown(
            "**Diretórios monitorados:**\n"
            "- `data/books/` (Manuais `.pdf`)\n"
            "- `data/lore/` (Cenário `.md`)\n"
            "- `data/npcs/` (Personagens `.md`)\n"
            "- `data/rules/` (Regras da Casa `.md`)"
        )

    # Inicializa histórico na sessão
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Renderiza histórico de mensagens
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["role"] == "assistant" and "sources" in message:
                render_sources_expander(message["sources"])

    # Entrada de pergunta do usuário
    user_input = st.chat_input(
        "Pergunte sobre regras, magias, cidades, deuses ou NPCs do compêndio..."
    )

    if user_input:
        # Exibe mensagem do usuário imediatamente
        with st.chat_message("user"):
            st.markdown(user_input)

        # Recupera os chunks mais relevantes no ChromaDB (4 a 6 chunks)
        retrieved_chunks = retrieve_context(
            query=user_input,
            n_results=top_k,
            category_filter=category_filter,
        )

        with st.chat_message("assistant"):
            if not api_key:
                assistant_reply = (
                    "⚠️ **Chave de API ausente:** Configure a variável `GEMINI_API_KEY` no arquivo `.env` "
                    "ou insira sua chave na barra lateral esquerda para que o Arquivista possa responder. "
                    "Abaixo estão os trechos recuperados localmente do ChromaDB para sua consulta:"
                )
                st.markdown(assistant_reply)
                render_sources_expander(retrieved_chunks)
            else:
                with st.spinner("Consultando os tomos antigos e manuais de regras..."):
                    try:
                        assistant_reply = generate_lorekeeper_answer(
                            api_key=api_key,
                            user_query=user_input,
                            retrieved_chunks=retrieved_chunks,
                            chat_history=st.session_state.messages,
                        )
                    except Exception as exc:
                        assistant_reply = (
                            f"❌ **Erro ao consultar a API do Gemini (`{MODEL_NAME}`):** `{exc}`\n\n"
                            "Verifique se sua `GEMINI_API_KEY` é válida. Os fragmentos relevantes encontrados "
                            "no banco vetorial local ainda podem ser inspecionados abaixo:"
                        )
                st.markdown(assistant_reply)
                render_sources_expander(retrieved_chunks)

        # Salva o turno no histórico
        st.session_state.messages.append({"role": "user", "content": user_input})
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": assistant_reply,
                "sources": retrieved_chunks,
            }
        )


if __name__ == "__main__":
    main()
