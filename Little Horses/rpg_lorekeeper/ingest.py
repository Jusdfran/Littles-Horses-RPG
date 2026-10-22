import hashlib
import logging
import math
import os
import pickle
import re
import sys
import types
import unicodedata
from collections import Counter
from pathlib import Path
from typing import List, Dict, Any, Tuple

import numpy as np

# Garante saída UTF-8 no console do Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


class PureNumpyHnswIndex:
    """
    Implementação vetorial em NumPy puro compatível com a interface hnswlib.Index
    do SegmentAPI do ChromaDB, evitando falhas de instrução nativa (AVX/DLL) em Windows.
    """

    file_handle_count = 1

    def __init__(self, space: str = "cosine", dim: int = 512):
        self.space = space
        self.dim = dim
        self.max_elements = 10000
        self.persistence_location = None
        self.is_persistent = False
        self._vectors: Dict[int, np.ndarray] = {}

    def init_index(
        self,
        max_elements: int = 10000,
        ef_construction: int = 200,
        M: int = 16,
        random_seed: int = 100,
        is_persistent_index: bool = False,
        persistence_location: str = None,
    ) -> None:
        self.max_elements = max_elements
        self.is_persistent = is_persistent_index
        self.persistence_location = persistence_location

    def load_index(
        self,
        path_to_index: str,
        is_persistent_index: bool = False,
        max_elements: int = 10000,
    ) -> None:
        self.persistence_location = path_to_index
        self.is_persistent = is_persistent_index
        self.max_elements = max_elements
        vec_file = os.path.join(path_to_index, "vectors.pkl")
        if os.path.exists(vec_file):
            with open(vec_file, "rb") as f:
                self._vectors = pickle.load(f)

    def set_ef(self, ef: int) -> None:
        pass

    def set_num_threads(self, num_threads: int) -> None:
        pass

    def get_max_elements(self) -> int:
        return self.max_elements

    def resize_index(self, new_size: int) -> None:
        self.max_elements = new_size

    def add_items(
        self, data, ids=None, num_threads: int = -1, replace_deleted: bool = False
    ) -> None:
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if ids is None:
            ids = list(range(len(arr)))
        for vec, label in zip(arr, ids):
            self._vectors[int(label)] = vec.copy()

    def mark_deleted(self, label: int) -> None:
        self._vectors.pop(int(label), None)

    def get_items(self, ids=None, return_type: str = "list") -> np.ndarray:
        if ids is None:
            ids = list(self._vectors.keys())
        out = [
            self._vectors.get(int(i), np.zeros(self.dim, dtype=np.float32))
            for i in ids
        ]
        return np.array(out, dtype=np.float32)

    def knn_query(self, data, k: int = 1, num_threads: int = -1, filter=None):
        queries = np.asarray(data, dtype=np.float32)
        if queries.ndim == 1:
            queries = queries.reshape(1, -1)

        valid_labels = [
            lbl for lbl in self._vectors.keys() if (filter is None or filter(lbl))
        ]
        if not valid_labels or k <= 0:
            return (
                np.empty((len(queries), 0), dtype=np.int64),
                np.empty((len(queries), 0), dtype=np.float32),
            )

        mat = np.stack([self._vectors[lbl] for lbl in valid_labels], axis=0)
        all_labels = []
        all_dists = []

        for q in queries:
            if self.space == "cosine":
                q_norm = np.linalg.norm(q) + 1e-10
                m_norms = np.linalg.norm(mat, axis=1) + 1e-10
                sims = (mat @ q) / (m_norms * q_norm)
                dists = 1.0 - sims
            elif self.space == "ip":
                dists = 1.0 - (mat @ q)
            else:
                diff = mat - q
                dists = np.sum(diff * diff, axis=1)

            top_k = min(k, len(valid_labels))
            idx = np.argsort(dists)[:top_k]
            all_labels.append([valid_labels[i] for i in idx])
            all_dists.append([float(dists[i]) for i in idx])

        return (
            np.array(all_labels, dtype=np.int64),
            np.array(all_dists, dtype=np.float32),
        )

    def persist_dirty(self) -> None:
        if self.is_persistent and self.persistence_location:
            os.makedirs(self.persistence_location, exist_ok=True)
            vec_file = os.path.join(self.persistence_location, "vectors.pkl")
            with open(vec_file, "wb") as f:
                pickle.dump(self._vectors, f, protocol=pickle.HIGHEST_PROTOCOL)

    def open_file_handles(self) -> None:
        pass

    def close_file_handles(self) -> None:
        pass


# Registra o módulo hnswlib em memória antes da importação do SegmentAPI do ChromaDB
if "hnswlib" not in sys.modules:
    _hnsw_mod = types.ModuleType("hnswlib")
    _hnsw_mod.Index = PureNumpyHnswIndex
    sys.modules["hnswlib"] = _hnsw_mod

import chromadb
from chromadb.config import Settings
from chromadb.api.types import EmbeddingFunction, Documents, Embeddings
from chromadb.utils.embedding_functions import register_embedding_function
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Configuração de logs clara para o console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("LorekeeperIngest")

# Caminhos base relativos ao diretório do projeto
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_DIR = BASE_DIR / "rpg_db"
COLLECTION_NAME = "rpg_lorekeeper"

# Categorias esperadas na estrutura de pastas
EXPECTED_CATEGORIES = ["books", "lore", "npcs", "rules"]

# Extensões suportadas
SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf"}

# Stopwords comuns em português para dar maior peso semântico aos termos de RPG
PT_STOPWORDS = {
    "a", "ao", "aos", "aquela", "aquelas", "aquele", "aqueles", "aquilo", "as",
    "ate", "com", "como", "da", "das", "de", "dela", "delas", "dele", "deles",
    "depois", "do", "dos", "e", "ela", "elas", "ele", "eles", "em", "entre",
    "era", "eram", "essa", "essas", "esse", "esses", "esta", "estas", "este",
    "estes", "eu", "foi", "for", "foram", "ha", "isso", "isto", "ja", "la",
    "lhe", "lhes", "mais", "mas", "me", "mesmo", "meu", "meus", "minha",
    "minhas", "muito", "na", "nas", "nao", "nem", "no", "nos", "nossa",
    "nossas", "nosso", "nossos", "num", "numa", "o", "os", "ou", "para",
    "pela", "pelas", "pelo", "pelos", "por", "qual", "quando", "que", "quem",
    "se", "seja", "sem", "seu", "seus", "so", "sua", "suas", "sao", "tambem",
    "te", "tem", "tendo", "tenho", "ter", "teu", "teus", "tu", "tua", "tuas",
    "um", "uma", "umas", "uns", "voce", "voces", "vos", "ser", "esta", "estao",
}


def _strip_accents(text: str) -> str:
    """Remove acentos mantendo caracteres base para matching resiliente."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


@register_embedding_function
class LorekeeperEmbeddingFunction(EmbeddingFunction[Documents]):
    """
    Função de Embeddings Híbrida Local (Unigramas + Bigramas + N-gramas Morfológicos)
    projetada via CountSketch determinístico em 512 dimensões normalizadas (L2).
    100% local, rápida e imune a falhas de DLL no Windows.
    """

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _embed_single(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        if not text or not text.strip():
            vec[0] = 1.0
            return vec

        clean = _strip_accents(text.lower())
        tokens = re.findall(r"[a-z0-9]+", clean)
        meaningful_tokens = [t for t in tokens if len(t) > 1 and t not in PT_STOPWORDS]

        features: Counter = Counter()

        # 1. Palavras-chave (Unigramas) com peso alto (3.0)
        for tok in meaningful_tokens:
            features[f"w:{tok}"] += 3.0
            # 2. Radicais e prefixos/sufixos morfológicos (3, 4 e 5-gramas de caracteres)
            padded = f"<{tok}>"
            for n in (3, 4, 5):
                if len(padded) >= n:
                    for i in range(len(padded) - n + 1):
                        features[f"c{n}:{padded[i:i+n]}"] += 0.8

        # 3. Expressões compostas (Bigramas de termos adjacentes, ex: 'lanterna prata', 'acerto critico')
        for i in range(len(meaningful_tokens) - 1):
            bigram = f"b:{meaningful_tokens[i]}_{meaningful_tokens[i+1]}"
            features[bigram] += 2.5

        for feat, raw_weight in features.items():
            # Escala sublinear TF
            weight = (1.0 + math.log1p(raw_weight))
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            h_val = int.from_bytes(digest, "little")
            idx1 = h_val % self.dim
            sign1 = 1.0 if ((h_val >> 32) & 1) == 0 else -1.0
            idx2 = (h_val >> 16) % self.dim
            sign2 = 1.0 if ((h_val >> 48) & 1) == 0 else -1.0
            vec[idx1] += sign1 * weight
            vec[idx2] += sign2 * (weight * 0.5)

        norm = float(np.linalg.norm(vec))
        if norm > 1e-9:
            vec /= norm
        else:
            vec[0] = 1.0
        return vec

    def __call__(self, input: Documents) -> Embeddings:
        return [self._embed_single(doc) for doc in input]

    @staticmethod
    def name() -> str:
        return "lorekeeper_hybrid_ef"

    def get_config(self) -> Dict[str, Any]:
        return {"dim": self.dim}

    @staticmethod
    def build_from_config(config: Dict[str, Any]) -> "LorekeeperEmbeddingFunction":
        return LorekeeperEmbeddingFunction(dim=config.get("dim", 512))


def get_chroma_collection():
    """
    Inicializa o cliente persistente ChromaDB em rpg_db/ e retorna a coleção
    configurada com a função de embeddings local do Lorekeeper.
    """
    ensure_directories()
    client = chromadb.PersistentClient(
        path=str(DB_DIR),
        settings=Settings(
            chroma_api_impl="chromadb.api.segment.SegmentAPI",
            anonymized_telemetry=False,
        ),
    )
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=LorekeeperEmbeddingFunction(),
        metadata={"description": "Compêndio de RPG Lorekeeper - Manuais, Lore, NPCs e Regras"},
    )


def ensure_directories() -> None:
    """Garante que toda a árvore de diretórios necessária exista."""
    for category in EXPECTED_CATEGORIES:
        (DATA_DIR / category).mkdir(parents=True, exist_ok=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def get_category(file_path: Path) -> str:
    """Determina a categoria com base na subpasta dentro de data/."""
    try:
        rel_parts = file_path.relative_to(DATA_DIR).parts
        if len(rel_parts) > 1:
            return rel_parts[0].lower()
    except ValueError:
        pass
    return "general"


def get_text_splitter() -> RecursiveCharacterTextSplitter:
    """Retorna o splitter configurado segundo a especificação técnica."""
    return RecursiveCharacterTextSplitter(
        chunk_size=700,
        chunk_overlap=120,
        separators=["\n\n", "\n", ". ", " ", ""],
    )


def process_pdf_file(
    file_path: Path, splitter: RecursiveCharacterTextSplitter
) -> Tuple[List[str], List[str], List[Dict[str, Any]]]:
    """
    Extrai texto página a página de um arquivo PDF usando pypdf.PdfReader e divide em chunks.
    Trata páginas escaneadas ou sem texto extraível emitindo warning no console.
    """
    ids: List[str] = []
    documents: List[str] = []
    metadatas: List[Dict[str, Any]] = []

    filename = file_path.name
    category = get_category(file_path)

    try:
        reader = PdfReader(str(file_path))
    except (PdfReadError, Exception) as exc:
        logger.warning(
            f"⚠️ Não foi possível abrir ou ler o PDF '{filename}': {exc}. Ignorando arquivo."
        )
        return ids, documents, metadatas

    total_pages = len(reader.pages)
    logger.info(
        f"📄 Processando PDF '{filename}' ({total_pages} páginas) [categoria: {category}]..."
    )

    for page_idx, page_obj in enumerate(reader.pages):
        page_number = page_idx + 1
        try:
            raw_text = page_obj.extract_text() or ""
            cleaned_text = raw_text.strip()

            if not cleaned_text:
                logger.warning(
                    f"⚠️ [{filename} - Página {page_number}] Página vazia ou escaneada (sem texto extraível). Pulando página."
                )
                continue

            chunks = splitter.split_text(cleaned_text)
            for chunk_idx, chunk in enumerate(chunks):
                doc_id = f"{filename}_p{page_number}_c{chunk_idx}"
                ids.append(doc_id)
                documents.append(chunk)
                metadatas.append(
                    {
                        "source": filename,
                        "page": str(page_number),
                        "category": category,
                    }
                )

        except Exception as exc:
            logger.warning(
                f"⚠️ [{filename} - Página {page_number}] Erro ao extrair texto da página: {exc}. Pulando página."
            )
            continue

    return ids, documents, metadatas


def process_text_or_md_file(
    file_path: Path, splitter: RecursiveCharacterTextSplitter
) -> Tuple[List[str], List[str], List[Dict[str, Any]]]:
    """
    Processa arquivos Markdown (.md) ou Texto (.txt), dividindo em chunks
    com page='N/A'.
    """
    ids: List[str] = []
    documents: List[str] = []
    metadatas: List[Dict[str, Any]] = []

    filename = file_path.name
    category = get_category(file_path)
    page_val = "N/A"

    try:
        content = file_path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        try:
            content = file_path.read_text(encoding="latin-1").strip()
        except Exception as exc:
            logger.warning(
                f"⚠️ Erro de codificação ao ler '{filename}': {exc}. Ignorando."
            )
            return ids, documents, metadatas
    except Exception as exc:
        logger.warning(f"⚠️ Erro ao ler o arquivo '{filename}': {exc}. Ignorando.")
        return ids, documents, metadatas

    if not content:
        logger.warning(f"⚠️ O arquivo '{filename}' está vazio. Ignorando.")
        return ids, documents, metadatas

    logger.info(f"📝 Processando documento '{filename}' [categoria: {category}]...")
    chunks = splitter.split_text(content)

    for chunk_idx, chunk in enumerate(chunks):
        doc_id = f"{filename}_p{page_val}_c{chunk_idx}"
        ids.append(doc_id)
        documents.append(chunk)
        metadatas.append(
            {
                "source": filename,
                "page": page_val,
                "category": category,
            }
        )

    return ids, documents, metadatas


def ingest_all() -> int:
    """
    Percorre recursivamente o diretório data/, processa todos os arquivos
    suportados (.md, .txt, .pdf) e realiza upsert idempotente no ChromaDB.
    Retorna o total de fragmentos indexados.
    """
    ensure_directories()
    splitter = get_text_splitter()

    logger.info(f"Inicializando ChromaDB persistente em: {DB_DIR}")
    collection = get_chroma_collection()

    files_to_process = sorted(
        [
            p
            for p in DATA_DIR.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
    )

    existing_records = collection.get(include=[])
    existing_ids = set(existing_records.get("ids", []))

    if not files_to_process:
        if existing_ids:
            collection.delete(ids=list(existing_ids))
            logger.info(
                f"🧹 Diretório data/ vazio: {len(existing_ids)} fragmentos antigos removidos do ChromaDB."
            )
        logger.warning(
            f"⚠️ Nenhum arquivo (.md, .txt, .pdf) encontrado em {DATA_DIR}. Aguardando envio de livros PDF ou criação de conteúdo."
        )
        return 0

    total_chunks = 0
    total_files = 0
    current_valid_ids = set()

    for file_path in files_to_process:
        ext = file_path.suffix.lower()
        if ext == ".pdf":
            ids, docs, metas = process_pdf_file(file_path, splitter)
        else:
            ids, docs, metas = process_text_or_md_file(file_path, splitter)

        if ids:
            current_valid_ids.update(ids)
            batch_size = 250
            for start in range(0, len(ids), batch_size):
                end = start + batch_size
                collection.upsert(
                    ids=ids[start:end],
                    documents=docs[start:end],
                    metadatas=metas[start:end],
                )
            total_chunks += len(ids)
            total_files += 1
            logger.info(
                f"✅ '{file_path.name}': {len(ids)} fragmentos indexados via upsert."
            )

    stale_ids = list(existing_ids - current_valid_ids)
    if stale_ids:
        collection.delete(ids=stale_ids)
        logger.info(f"🧹 Removidos {len(stale_ids)} fragmentos obsoletos do ChromaDB.")

    logger.info(
        f"🎉 Ingestão concluída com sucesso! "
        f"Arquivos processados: {total_files} | "
        f"Fragmentos ativos na coleção '{COLLECTION_NAME}': {collection.count()}"
    )
    return total_chunks


if __name__ == "__main__":
    ingest_all()
